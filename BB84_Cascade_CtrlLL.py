"""
BB84 Quantum Key Distribution — 2-Pass Cascade Error Reconciliation
Standard library only: secrets, math, random (no numpy required)
Matches the exact architectural design of the LDPC implementation.
"""

import secrets
import math
import random

# ---------------------------------------------------------------------------
# 1. CONFIGURATION & SECURE RANDOMNESS
# ---------------------------------------------------------------------------

BASES = ['+', 'x']

def random_bit():
    return secrets.randbelow(2)

def random_base():
    return secrets.choice(BASES)

def measure(bit, send_base, recv_base):
    """Matching bases → deterministic; mismatched → random (quantum uncertainty)."""
    return bit if send_base == recv_base else random_bit()


# ---------------------------------------------------------------------------
# 2. QUANTUM CHANNEL  (BB84 — Identical to LDPC Layer)
# ---------------------------------------------------------------------------

def alice_prepare(n):
    bits  = [random_bit()  for _ in range(n)]
    bases = [random_base() for _ in range(n)]
    return bits, bases


def eve_intercept(alice_bits, alice_bases, attack_fraction=0.20):
    """Partial intercept-resend attack on `attack_fraction` of qubits."""
    pool     = list(range(len(alice_bits)))
    attacked = set()
    for _ in range(int(len(alice_bits) * attack_fraction)):
        idx = secrets.randbelow(len(pool))
        attacked.add(pool.pop(idx))

    channel_bits  = alice_bits[:]
    channel_bases = alice_bases[:]
    for i in attacked:
        eve_base         = random_base()
        channel_bits[i]  = measure(alice_bits[i], alice_bases[i], eve_base)
        channel_bases[i] = eve_base
    return channel_bits, channel_bases


def bob_measure(channel_bits, channel_bases):
    bob_bases   = [random_base() for _ in range(len(channel_bits))]
    bob_results = [
        measure(bit, prep, bob)
        for bit, prep, bob in zip(channel_bits, channel_bases, bob_bases)
    ]
    return bob_results, bob_bases


def sift(alice_bits, alice_bases, bob_results, bob_bases):
    """Keep only positions where Alice and Bob chose the same basis."""
    sifted_a, sifted_b = [], []
    for ab, ab_base, br, bb_base in zip(alice_bits, alice_bases, bob_results, bob_bases):
        if ab_base == bb_base:
            sifted_a.append(ab)
            sifted_b.append(br)
    return sifted_a, sifted_b


# ---------------------------------------------------------------------------
# 3. CLASSICAL LAYER — 2-PASS CASCADE RECONCILIATION
#
# Protocol:
#   Unlike the one-way syndrome transmission of LDPC, Cascade is highly interactive.
#   
#   Pass 1: Key is broken into linear blocks. Parities are compared. 
#           Mismatches trigger binary search to fix single bit errors.
#           Leaves even numbers of errors untouched within blocks.
#
#   Pass 2: A pseudo-random permutation shuffles indices using a shared seed.
#           The block size is doubled. This separates error pairs into independent
#           blocks, allowing Pass 2 to catch remaining even error profiles.
# ---------------------------------------------------------------------------

def _binary_search_correction(alice_block, bob_block):
    """
    Dichotomic binary search to find a single bit error in an odd-parity block.
    Simulates interactive back-and-forth communication.
    """
    if len(alice_block) == 1:
        return 0

    mid = len(alice_block) // 2
    left_a, left_b = alice_block[:mid], bob_block[:mid]

    # If left half parity is different, error is in left half
    if sum(left_a) % 2 != sum(left_b) % 2:
        return _binary_search_correction(left_a, left_b)
    else:
        # Error is in the right half
        return mid + _binary_search_correction(alice_block[mid:], bob_block[mid:])


def reconcile_cascade(alice_key, bob_key, qber):
    """
    2-Pass Cascade Error Reconciliation Driver.
    Tracks information leakage and interaction round-trips.
    
    Returns: corrected_bob_key, bits_leaked, completed_passes, total_round_trips
    """
    n = len(alice_key)
    if n == 0:
        return [], 0, 0, 0

    corrected_b = bob_key[:]
    total_leakage = 0
    total_round_trips = 0

    # Calculate optimal Pass 1 block size based on QBER
    p = max(1e-9, min(0.5, qber))
    k1 = max(2, math.ceil(1.0 / p)) if qber > 0 else n

    # -----------------------------------------------------------------------
    # PASS 1: Linear Blocks
    # -----------------------------------------------------------------------
    for i in range(0, n, k1):
        block_a = alice_key[i : i + k1]
        block_b = corrected_b[i : i + k1]

        total_leakage += 1        # 1 bit leaked for initial parity check
        total_round_trips += 1    # One communication frame exchanged

        if sum(block_a) % 2 != sum(block_b) % 2:
            # Odd error count detected -> perform binary search
            error_idx = _binary_search_correction(block_a, block_b)
            abs_idx = i + error_idx
            if abs_idx < n:
                corrected_b[abs_idx] ^= 1 # Correct bit in place
            
            # Add binary search leakage and rounds
            block_len = len(block_a)
            search_steps = math.ceil(math.log2(block_len))
            total_leakage += search_steps
            total_round_trips += search_steps

    # -----------------------------------------------------------------------
    # PASS 2: Shuffled Blocks (Decoupling Error Pairs)
    # -----------------------------------------------------------------------
    # Alice and Bob agree on a synchronized deterministic seed for shuffling
    shuffle_seed = 12345
    rng = random.Random(shuffle_seed)
    
    perm_indices = list(range(n))
    rng.shuffle(perm_indices)

    # Map the keys to the shuffled domain
    shuffled_a = [alice_key[idx] for idx in perm_indices]
    shuffled_b = [corrected_b[idx] for idx in perm_indices]

    # Rule of Cascade: Pass 2 block size is typically doubled
    k2 = k1 * 2

    for i in range(0, n, k2):
        block_a = shuffled_a[i : i + k2]
        block_b = shuffled_b[i : i + k2]

        total_leakage += 1
        total_round_trips += 1

        if sum(block_a) % 2 != sum(block_b) % 2:
            error_idx = _binary_search_correction(block_a, block_b)
            
            # Correct the error in the shuffled array
            shuffled_b[i + error_idx] ^= 1
            
            # Update the master index list
            original_idx = perm_indices[i + error_idx]
            corrected_b[original_idx] ^= 1

            block_len = len(block_a)
            search_steps = math.ceil(math.log2(block_len))
            total_leakage += search_steps
            total_round_trips += search_steps

    return corrected_b, total_leakage, 2, total_round_trips


# ---------------------------------------------------------------------------
# 4. SIMULATION DRIVER
# ---------------------------------------------------------------------------

def run_bb84_cascade(n_bits=1000, attack_fraction=0.20):
    print("=" * 58)
    print("  BB84 + 2-PASS CASCADE QKD SIMULATION")
    print(f"  Raw qubits : {n_bits}   Eve attack : {attack_fraction:.0%}")
    print("=" * 58)

    # ── Phase 1: Quantum channel ────────────────────────────────────────
    print("\n[PHASE 1 — QUANTUM CHANNEL]")
    a_bits,  a_bases   = alice_prepare(n_bits)
    c_bits,  c_bases   = eve_intercept(a_bits, a_bases, attack_fraction)
    b_results, b_bases = bob_measure(c_bits, c_bases)
    sifted_a, sifted_b = sift(a_bits, a_bases, b_results, b_bases)

    n_sifted   = len(sifted_a)
    raw_errors = sum(a != b for a, b in zip(sifted_a, sifted_b))
    qber       = raw_errors / n_sifted if n_sifted else 0.0

    print(f"  Sifted length : {n_sifted} bits  (~{n_sifted/n_bits:.0%} of raw, expected ~50%)")
    print(f"  Raw errors    : {raw_errors}")
    print(f"  QBER          : {qber:.2%}  (expected ~{attack_fraction*0.25:.2%} from a {attack_fraction:.0%} attack)")

    THRESHOLD = 0.11
    if qber > THRESHOLD:
        print(f"\n  [!] QBER {qber:.2%} > {THRESHOLD:.0%} threshold — eavesdropping detected, aborting.")
        return

    # ── Phase 2: Cascade reconciliation ─────────────────────────────────
    print("\n[PHASE 2 — CASCADE RECONCILIATION]")
    corrected_b, leaked, completed_passes, rounds = reconcile_cascade(sifted_a, sifted_b, qber)
    final_errors = sum(a != b for a, b in zip(sifted_a, corrected_b))

    print(f"  Completed passes : {completed_passes}")
    print(f"  Network rounds   : {rounds} network frames exchanged")
    print(f"  Bits leaked      : {leaked}  ({leaked/n_sifted:.2%} of sifted key)")
    print(f"  Remaining err    : {final_errors}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n[SUMMARY]")
    if final_errors == 0:
        secure_len = max(0, n_sifted - leaked)
        print("  [+] SUCCESS — Alice and Bob hold identical keys.")
        print(f"  Estimated secure key length (post privacy amplification) : ~{secure_len} bits")
    else:
        print(f"  [!] {final_errors} error(s) remain after 2-Pass Cascade.")
        print("      Highlight: Multi-pass loops are trapped by even error counts.")


if __name__ == "__main__":
    run_bb84_cascade()