"""Generate the randomized hub/port plan of run 2 and print it as TOML.

Design (see docs/test_plan_run2.md):

* 20 drives under test: 10 ABLAZE 16 GB (A16), 8 ABLAZE 8 GB (A8), 2 SanDisk 8 GB (S8).
  Two of the ten new ABLAZE 8 GB drives are drawn at random as spares; they are not
  tested and are used for the self-test before the start and for port checks after it.
* Two 10-port hubs form two blocks. Each hub carries the same mix
  (5 A16 + 4 A8 + 1 S8), so a hub effect cannot be mistaken for a capacity or brand
  effect.
* Within a hub, the ten drives are placed on the ten ports in random order, because
  the ports of a 10-port hub sit behind different internal hub chips.

The plan is printed once and pasted into config/campaign.toml, so the operator sees a
fixed plan; the seed makes it reproducible.

    python tools/make_assignment.py
"""

from __future__ import annotations

import random

SEED = 20261116
A16 = [f"A16-{i:02d}" for i in range(3, 13)]  # continues run 1 (A16-01, A16-02)
A8 = [f"A8-{i:02d}" for i in range(6, 16)]     # continues run 1 (A8-01 ... A8-05)
S8 = ["S8-01", "S8-02"]                        # reused from run 1


def make_plan(seed: int = SEED) -> tuple[dict[str, list[str]], list[str]]:
    rng = random.Random(seed)
    spares = sorted(rng.sample(A8, 2))
    a8_test = [d for d in A8 if d not in spares]

    def split(ids: list[str]) -> tuple[list[str], list[str]]:
        ids = list(ids)
        rng.shuffle(ids)
        half = len(ids) // 2
        return ids[:half], ids[half:]

    a16_a, a16_b = split(A16)
    a8_a, a8_b = split(a8_test)
    s8_a, s8_b = split(S8)
    hubs = {"A": a16_a + a8_a + s8_a, "B": a16_b + a8_b + s8_b}
    for drives in hubs.values():
        rng.shuffle(drives)
    return hubs, spares


def main() -> None:
    hubs, spares = make_plan()
    print(f"# Randomized plan, seed {SEED} (tools/make_assignment.py). Do not edit by hand.")
    print(f"spares = {spares!r}".replace("'", '"'))
    print()
    for hub, drives in hubs.items():
        for port, drive in enumerate(drives, start=1):
            print("[[slot]]")
            print(f'hub = "{hub}"')
            print(f"port = {port}")
            print(f'drive = "{drive}"')
            print()


if __name__ == "__main__":
    main()
