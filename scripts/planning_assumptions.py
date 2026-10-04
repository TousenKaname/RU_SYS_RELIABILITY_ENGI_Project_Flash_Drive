"""Speed assumptions that size the test until the pilot measures real values.

Sustained (write, read) throughput in MB/s for each workload. ``NOMINAL`` is
the planning case; ``PESSIMISTIC`` halves every write speed. After the pilot,
replace both with the measured medians and regenerate the figures and tables.
"""

#: All nine drives cycle from 6 Oct 09:00 to the stop time, 18 Oct 09:00.
TEST_DAYS = 12

NOMINAL = {"small": (1.0, 6.0), "medium": (6.0, 18.0), "large": (7.0, 20.0)}
PESSIMISTIC = {w: (write / 2, read) for w, (write, read) in NOMINAL.items()}
