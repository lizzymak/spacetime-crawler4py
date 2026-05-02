SIMHASH_BITS = 64


def stable_hash_64(text):
    """
    FNV-1a 64-bit hash implemented from scratch.
    """
    hash_value = 1469598103934665603
    fnv_prime = 1099511628211

    for char in text:
        hash_value ^= ord(char)
        hash_value = (hash_value * fnv_prime) & 0xffffffffffffffff

    return hash_value


def compute_simhash(tokens):
    """
    SimHash implementation from scratch.
    """
    vector = [0] * SIMHASH_BITS

    for token in tokens:
        token_hash = stable_hash_64(token)

        for bit_index in range(SIMHASH_BITS):
            bit = (token_hash >> bit_index) & 1

            if bit == 1:
                vector[bit_index] += 1
            else:
                vector[bit_index] -= 1

    fingerprint = 0

    for bit_index in range(SIMHASH_BITS):
        if vector[bit_index] >= 0:
            fingerprint |= (1 << bit_index)

    return fingerprint


def hamming_distance(hash1, hash2):
    """
    Count how many bits differ between two SimHashes.
    """
    value = hash1 ^ hash2
    distance = 0

    while value:
        distance += value & 1
        value >>= 1

    return distance