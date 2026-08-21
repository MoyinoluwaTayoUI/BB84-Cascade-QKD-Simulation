"""
BB84 Quantum Key Distribution — LDPC Belief-Propagation Reconciliation
Standard library only: secrets, math, random  (no numpy required)
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
# 2. QUANTUM CHANNEL  (BB84)
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
# 3. CLASSICAL LAYER — LDPC BELIEF-PROPAGATION RECONCILIATION
#
#  Protocol:
#   Alice holds ground-truth key x (length n).
#   Bob   holds noisy version    y (QBER fraction of bits differ).
#
#   (A) Both parties derive an identical sparse parity-check matrix H (m×n)
#       from a shared seed — no extra communication needed.
#   (B) Alice computes syndrome s = H·x (mod 2) and sends s to Bob.
#       This leaks exactly m bits to any eavesdropper.
#   (C) Bob decodes the error pattern e = x XOR y via sum-product belief
#       propagation, then recovers x = y XOR e.
#
#  Code rate r = 1 − m/n.  For a BSC with crossover p, Shannon capacity is
#  C = 1 − H_b(p).  We target r = 0.70 × C, giving enough redundancy for
#  reliable short-block convergence without an optimised matrix construction.
# ---------------------------------------------------------------------------

def _build_H(n, m, col_weight=3, seed=42):
    """
    Sparse parity-check matrix H (m×n), regular column-weight construction.
    Both parties call with the same seed → identical H with no extra communication.
    Returns H  (list of sets: row → column indices)
            Ht (list of lists: column → row indices)
    """
    rng            = random.Random(seed)
    max_row_weight = max(4, math.ceil(n * col_weight / m) + 2)
    H  = [set() for _ in range(m)]
    Ht = [[]    for _ in range(n)]
    for j in range(n):
        weights    = [len(H[i]) for i in range(m)]
        candidates = [i for i in range(m) if weights[i] < max_row_weight] or list(range(m))
        for i in rng.sample(candidates, min(col_weight, len(candidates))):
            H[i].add(j)
            Ht[j].append(i)
    return H, Ht


def _syndrome(H, bits):
    """Compute s = H · bits (mod 2)."""
    return [int(sum(bits[j] for j in row) % 2) for row in H]


def _belief_propagation(H, Ht, target_syn, bob_bits, qber, max_iter=100):
    """
    Sum-product BP decoding on a BSC.
    Decodes the error pattern e s.t. H·e = target_syn, then returns y XOR e.

    Channel LLR for each position: log P(eⱼ=0)/P(eⱼ=1) = log((1−p)/p).
    Errors are i.i.d. on a BSC, so this is the same for every bit.
    """
    n, m, eps = len(bob_bits), len(H), 1e-10
    ch_llr    = [math.log((1 - qber) / (qber + eps))] * n   # positive → likely no error

    v2c       = {j: {i: ch_llr[j] for i in Ht[j]} for j in range(n)}
    c2v       = {i: {j: 0.0       for j in H[i]}  for i in range(m)}
    total_llr = ch_llr[:]

    for it in range(1, max_iter + 1):
        # Check-node update (sum-product via tanh product)
        for i in range(m):
            nb   = list(H[i])
            sign = -1 if target_syn[i] else 1
            for j in nb:
                prod = sign
                for k in nb:
                    if k != j:
                        t    = math.tanh(v2c[k][i] / 2.0)
                        prod *= max(-1 + eps, min(1 - eps, t))
                c2v[i][j] = 2.0 * math.atanh(max(-1 + eps, min(1 - eps, prod)))

        # Variable-node update
        for j in range(n):
            total_llr[j] = ch_llr[j] + sum(c2v[i][j] for i in Ht[j])
            for i in Ht[j]:
                v2c[j][i] = ch_llr[j] + sum(c2v[k][j] for k in Ht[j] if k != i)

        # Hard decision + syndrome check
        e_hat = [1 if total_llr[j] < 0 else 0 for j in range(n)]
        if _syndrome(H, e_hat) == target_syn:
            return [b ^ e for b, e in zip(bob_bits, e_hat)], True, it

    e_hat = [1 if total_llr[j] < 0 else 0 for j in range(n)]
    return [b ^ e for b, e in zip(bob_bits, e_hat)], False, max_iter


def reconcile_ldpc(alice_key, bob_key, qber, col_weight=3, max_iter=100):
    """
    LDPC reconciliation driver.
    Alice sends the syndrome (m bits) to Bob, who runs BP to recover alice_key.

    Returns: corrected_bob_key, bits_leaked, converged, iterations_used
    """
    n   = len(alice_key)
    eps = 1e-9
    p   = max(eps, min(0.5 - eps, qber))
    h_b = -p * math.log2(p) - (1 - p) * math.log2(1 - p)

    # 0.70 × capacity gives a conservative rate that reliably converges
    # at short block lengths without a factory-optimised LDPC matrix.
    rate = max(0.10, min(0.90, 0.70 * (1.0 - h_b)))
    m    = max(1, min(n - 1, math.ceil(n * (1.0 - rate))))

    H, Ht     = _build_H(n, m, col_weight=col_weight)
    alice_syn = _syndrome(H, alice_key)          # Alice computes & sends this
    bob_syn   = _syndrome(H, bob_key)
    target    = [a ^ b for a, b in zip(alice_syn, bob_syn)]   # = H·(x XOR y)

    corrected, converged, iters = _belief_propagation(H, Ht, target, bob_key, qber, max_iter)
    return corrected, m, converged, iters


# ---------------------------------------------------------------------------
# 4. SIMULATION DRIVER
# ---------------------------------------------------------------------------

def run_bb84_ldpc(n_bits=1000, attack_fraction=0.20):
    print("=" * 58)
    print("  BB84 + LDPC BELIEF-PROPAGATION QKD SIMULATION")
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

    # ── Phase 2: LDPC reconciliation ────────────────────────────────────
    print("\n[PHASE 2 — LDPC BELIEF-PROPAGATION RECONCILIATION]")
    corrected_b, leaked, converged, iters = reconcile_ldpc(sifted_a, sifted_b, qber)
    final_errors = sum(a != b for a, b in zip(sifted_a, corrected_b))

    print(f"  BP converged  : {'YES — syndrome matched exactly' if converged else 'NO  — best estimate applied'}")
    print(f"  Iterations    : {iters}")
    print(f"  Bits leaked   : {leaked}  ({leaked/n_sifted:.2%} of sifted key)")
    print(f"  Remaining err : {final_errors}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n[SUMMARY]")
    if final_errors == 0:
        secure_len = max(0, n_sifted - leaked)
        print("  [+] SUCCESS — Alice and Bob hold identical keys.")
        print(f"  Estimated secure key length (post privacy amplification) : ~{secure_len} bits")
    else:
        print(f"  [!] {final_errors} error(s) remain after reconciliation.")
        print("      Try: larger n_bits, more max_iter, or call reconcile_ldpc(..., col_weight=4).")


if __name__ == "__main__":
    run_bb84_ldpc()