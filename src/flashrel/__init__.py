"""flashrel: endurance testing and reliability analysis of USB flash drives.

The package has two halves that share one data format:

* the **test harness** (``config``, ``payload``, ``cycle``, ``failure``,
  ``recorder``, ``runner``, ``system``) drives repeated copy-verify-delete
  cycles on many drives in parallel and logs every cycle and event;
* the **analysis toolkit** (``analysis``, ``viz``) turns those logs into life
  tables, reliability estimates, degradation summaries and figures.

The harness needs only the standard library and PyYAML, so it runs on a
Windows PC, a Mac or a Raspberry Pi; the analysis extras add NumPy, SciPy,
pandas and Matplotlib.
"""

__version__ = "0.1.0"
