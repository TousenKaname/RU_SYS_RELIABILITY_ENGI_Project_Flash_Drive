"""Speed assumptions that size the test until the pilot measures real values.

Sustained (write, read) throughput in MB/s for each workload. ``NOMINAL`` is
the planning case; ``PESSIMISTIC`` halves every write speed. After the pilot,
replace both with the measured medians and regenerate the figures and tables.
"""

#: Phase 1 runs from 12 Oct 09:00 to 1 Dec 09:00.
TEST_DAYS = 50
#: The extension drives (Phase 2) start on 22 Oct, ten days after Phase 1.
EXTENSION_DELAY_DAYS = 10

NOMINAL = {"small": (1.0, 6.0), "medium": (6.0, 18.0), "large": (7.0, 20.0)}
PESSIMISTIC = {w: (write / 2, read) for w, (write, read) in NOMINAL.items()}
