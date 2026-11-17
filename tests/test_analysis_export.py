from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile

import pytest

from flashrel.analysis import chi2_quantile, kaplan_meier, weibayes_lower, weibull_mle
from flashrel.usbpath import location_key
from flashrel.xlsx import write_xlsx


def test_weibull_mle_matches_scipy():
    # reference: scipy.optimize on the censored log-likelihood (see tests/README)
    t = [3, 3, 6, 9, 12, 14, 20, 20, 20, 20]
    e = [1, 1, 1, 1, 1, 1, 0, 0, 0, 0]
    fit = weibull_mle(t, e)
    assert fit["beta"] == pytest.approx(1.1671758, rel=1e-6)
    assert fit["eta"] == pytest.approx(20.19377873, rel=1e-6)
    assert weibull_mle([5, 7], [1, 0]) is None  # needs two failures


def test_chi2_and_weibayes():
    for p, k, ref in ((0.9, 2, 4.60517019), (0.9, 4, 7.77944034), (0.9, 6, 10.64464068)):
        assert chi2_quantile(p, k) == pytest.approx(ref, rel=1e-6)
    # zero failures, shape 2: theta_L = sqrt(2 * sum t^2 / chi2(0.9, 2))
    assert weibayes_lower([26, 23], [0, 0], 2.0) == pytest.approx(((2 * (26**2 + 23**2)) / 4.60517019) ** 0.5)


def test_kaplan_meier_steps():
    steps = kaplan_meier([3, 3, 3, 6, 9, 10, 10], [1, 1, 1, 1, 1, 0, 0])
    assert [round(s["s"], 4) for s in steps] == [1.0, round(4 / 7, 4), round(4 / 7 * 3 / 4, 4), round(4 / 7 * 3 / 4 * 2 / 3, 4)]


def test_xlsx_is_well_formed(tmp_path):
    path = tmp_path / "w.xlsx"
    write_xlsx(str(path), [("Life table", ["drive", "cycles", "note"], [["A8-06", 3, "F1 <cycle 3> & more"], ["S8-01", 26.5, None]]),
                           ("Events", ["t"], [])])
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(z.read(name))
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "<v>26.5</v>" in sheet and "F1 &lt;cycle 3&gt; &amp; more" in sheet


def test_location_key():
    assert location_key(["PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)#USB(2)#USB(4)"]) == \
        ("PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)", "2.4")
    assert location_key(["ACPI(_SB_)#ACPI(PC02)#ACPI(XHCI)#ACPI(RHUB)#ACPI(HS01)#USB(1)"]) == \
        ("ACPI(_SB_)#ACPI(PC02)#ACPI(XHCI)#ACPI(RHUB)#ACPI(HS01)", "1")
    assert location_key([]) == ("", "")
