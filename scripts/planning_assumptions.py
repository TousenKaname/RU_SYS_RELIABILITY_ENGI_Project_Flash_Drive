"""Speed assumptions that size the test until the pilot measures real values.

Sustained (write, read) throughput in MB/s for each workload. ``NOMINAL`` is
the planning case; ``PESSIMISTIC`` halves every write speed. After the pilot,
replace both with the measured medians and regenerate the figures and tables.
"""

from datetime import datetime

#: All eight drives cycle from 28 Oct 12:00 to the stop time, 29 Oct 12:00.
TEST_DAYS = 1
#: The first day of the plan: intake at 09:00, pilot at 10:00, all drives at 12:00.
PLAN_DAY = datetime(2026, 10, 28)

NOMINAL = {"small": (1.0, 6.0), "medium": (6.0, 18.0), "large": (7.0, 20.0)}
PESSIMISTIC = {w: (write / 2, read) for w, (write, read) in NOMINAL.items()}
