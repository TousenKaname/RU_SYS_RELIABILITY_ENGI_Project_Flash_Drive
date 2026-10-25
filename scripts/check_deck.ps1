# Layout check of a deck through PowerPoint COM (read-only), beyond text-on-text:
#  1. text that overflows its own box (TextFrame2 bounds against the shape box);
#  2. text that overlaps other text, a picture, a chart, or a table;
#  3. anything past the slide edge, outside the side margins, or in the footer band;
#  4. inside native charts: data labels that overlap each other or the legend, a legend that
#     overlaps the plot area, and labels outside the chart area.
#   check_deck.ps1 -Deck deck.pptx
param([Parameter(Mandatory)][string]$Deck)
$Deck = (Resolve-Path $Deck).Path
$ErrorActionPreference = "Stop"   # no PowerPoint, no verdict
for ($try = 1; $try -le 5; $try++) {  # PowerPoint may still be closing after an export
    try { $app = New-Object -ComObject PowerPoint.Application; $pres = $app.Presentations.Open($Deck, $true, $false, $false); break }
    catch { if ($try -eq 5) { throw "PowerPoint COM unavailable: $_" }; Start-Sleep -Seconds 3 }
}
$ErrorActionPreference = "Continue"
$W = $pres.PageSetup.SlideWidth; $H = $pres.PageSetup.SlideHeight
$footerTop = 6.88 * 72         # the footer and slide number sit below this line
$marginL = 0.70 * 72; $marginR = $W - 0.70 * 72
$issues = New-Object System.Collections.Generic.List[string]
function Short($t, $n = 32) { $t = ($t -replace "\s+", " ").Trim(); if ($t.Length -gt $n) { $t.Substring(0, $n) + "..." } else { $t } }
function Rect($l, $t, $w, $h) { [pscustomobject]@{ L = $l; T = $t; R = $l + $w; B = $t + $h } }
function Overlap($a, $b, $tol = 1.0) {
    $ow = [math]::Min($a.R, $b.R) - [math]::Max($a.L, $b.L)
    $oh = [math]::Min($a.B, $b.B) - [math]::Max($a.T, $b.T)
    if ($ow -gt $tol -and $oh -gt $tol) { return "$([int]$ow)x$([int]$oh) pt" } else { return $null }
}
try {
    foreach ($slide in $pres.Slides) {
        $n = $slide.SlideIndex
        $cover = ($n -eq 1 -or $n -eq $pres.Slides.Count)
        $texts = @(); $blocks = @()
        foreach ($s in $slide.Shapes) {
            $box = Rect $s.Left $s.Top $s.Width $s.Height
            $isNumber = $s.Name -like "Slide Number*"
            if ($s.HasTable) {
                $t = $s.Table
                for ($r = 1; $r -le $t.Rows.Count; $r++) { for ($c = 1; $c -le $t.Columns.Count; $c++) {
                    $cs = $t.Cell($r, $c).Shape
                    if ($cs.HasTextFrame -and $cs.TextFrame2.HasText) {
                        $tf = $cs.TextFrame2; $tr = $tf.TextRange
                        $needW = $tr.BoundWidth + $tf.MarginLeft + $tf.MarginRight
                        $needH = $tr.BoundHeight + $tf.MarginTop + $tf.MarginBottom
                        if ($needW -gt $t.Columns.Item($c).Width + 1.0) { $issues.Add("slide ${n}: TABLE CELL too wide '$(Short $tr.Text)' needs $([int]$needW) of $([int]$t.Columns.Item($c).Width) pt") }
                        if ($needH -gt $t.Rows.Item($r).Height + 1.0) { $issues.Add("slide ${n}: TABLE CELL too tall '$(Short $tr.Text)' needs $([int]$needH) of $([int]$t.Rows.Item($r).Height) pt") }
                        $nl = $tr.Lines().Count
                        for ($k = 1; $k -lt $nl; $k++) {
                            $ln = $tr.Lines($k).Text.TrimEnd()
                            if ($ln -match "[A-Za-z0-9][-‐–]$") { $issues.Add("slide ${n}: HYPHEN BREAK in table '$(Short $ln 40)'") }
                        }
                    } } }
                $blocks += [pscustomobject]@{ Name = $s.Name; Kind = "table"; Rect = $box }
            } elseif ($s.HasChart) {
                $blocks += [pscustomobject]@{ Name = $s.Name; Kind = "chart"; Rect = $box }
                $ch = $s.Chart; $labels = @()
                $nser = $ch.SeriesCollection().Count
                for ($i = 1; $i -le $nser; $i++) {
                    $ser = $ch.SeriesCollection($i)
                    $npt = 0; try { $npt = $ser.Points().Count } catch { }
                    for ($k = 1; $k -le $npt; $k++) {
                        try {
                            $pt = $ser.Points($k)
                            if ($pt.HasDataLabel -and $pt.DataLabel.Text) {
                                $dl = $pt.DataLabel
                                $labels += [pscustomobject]@{ Text = "$($ser.Name)/$k '$($dl.Text)'"; Rect = (Rect $dl.Left $dl.Top $dl.Width $dl.Height) }
                            }
                        } catch { }
                    }
                }
                for ($a = 0; $a -lt $labels.Count; $a++) {
                    $L = $labels[$a].Rect
                    if ($L.L -lt -1 -or $L.T -lt -1 -or $L.R -gt $s.Width + 1 -or $L.B -gt $s.Height + 1) { $issues.Add("slide ${n}: CHART '$($s.Name)' label outside the chart area: $($labels[$a].Text)") }
                    for ($b = $a + 1; $b -lt $labels.Count; $b++) {
                        $o = Overlap $L $labels[$b].Rect
                        if ($o) { $issues.Add("slide ${n}: CHART '$($s.Name)' labels overlap: $($labels[$a].Text) <-> $($labels[$b].Text) ($o)") }
                    }
                }
                if ($ch.HasLegend) {
                    $lg = Rect $ch.Legend.Left $ch.Legend.Top $ch.Legend.Width $ch.Legend.Height
                    $pa = $ch.PlotArea; $inside = Rect $pa.InsideLeft $pa.InsideTop $pa.InsideWidth $pa.InsideHeight
                    $o = Overlap $lg $inside
                    if ($o) { $issues.Add("slide ${n}: CHART '$($s.Name)' legend overlaps the plot area ($o)") }
                    foreach ($lab in $labels) { $o = Overlap $lg $lab.Rect; if ($o) { $issues.Add("slide ${n}: CHART '$($s.Name)' legend overlaps label $($lab.Text) ($o)") } }
                }
            } elseif ($s.Type -eq 13) {
                $blocks += [pscustomobject]@{ Name = $s.Name; Kind = "picture"; Rect = $box }
            } elseif ($s.HasTextFrame -and $s.TextFrame2.HasText) {
                $tr = $s.TextFrame2.TextRange
                $tb = Rect $tr.BoundLeft $tr.BoundTop $tr.BoundWidth $tr.BoundHeight
                $texts += [pscustomobject]@{ Name = $s.Name; Text = $tr.Text; Rect = $tb; Box = $box; Number = $isNumber }
                if ($tb.B -gt $box.B + 1.5 -or $tb.T -lt $box.T - 1.5) { $issues.Add("slide ${n}: OVERFLOW (vertical) '$(Short $tr.Text)' text $([int]$tb.T)-$([int]$tb.B) vs box $([int]$box.T)-$([int]$box.B)") }
                if ($tb.R -gt $box.R + 1.5 -or $tb.L -lt $box.L - 1.5) { $issues.Add("slide ${n}: OVERFLOW (horizontal) '$(Short $tr.Text)' text $([int]$tb.L)-$([int]$tb.R) vs box $([int]$box.L)-$([int]$box.R)") }
                # a word or a drive ID split at its hyphen across two lines
                $nl = $tr.Lines().Count
                for ($k = 1; $k -lt $nl; $k++) {
                    $ln = $tr.Lines($k).Text.TrimEnd()
                    if ($ln -match "[A-Za-z0-9][-‐–]$") { $issues.Add("slide ${n}: HYPHEN BREAK '$(Short $ln 40)' / '$(Short $tr.Lines($k + 1).Text 20)'") }
                }
            }
            # edges, margins, footer band (the full-bleed panels and the cover art excepted)
            $full = $s.Name -like "*panel" -and $cover
            if (-not $full -and -not $isNumber -and $s.Type -ne 9) {
                if ($box.L -lt -0.5 -or $box.T -lt -0.5 -or $box.R -gt $W + 0.5 -or $box.B -gt $H + 0.5) { $issues.Add("slide ${n}: OFF SLIDE '$($s.Name)'") }
                elseif (-not $cover -and ($box.L -lt $marginL - 0.5 -or $box.R -gt $marginR + 0.5)) { $issues.Add("slide ${n}: OUTSIDE MARGINS '$($s.Name)' $([int]$box.L)-$([int]$box.R) pt") }
                if (-not $cover -and $box.B -gt $footerTop) { $issues.Add("slide ${n}: IN FOOTER BAND '$($s.Name)' bottom $([math]::Round($box.B / 72, 2)) in") }
            }
        }
        for ($a = 0; $a -lt $texts.Count; $a++) {
            for ($b = $a + 1; $b -lt $texts.Count; $b++) {
                $o = Overlap $texts[$a].Rect $texts[$b].Rect
                if ($o) { $issues.Add("slide ${n}: TEXT OVERLAP '$(Short $texts[$a].Text 24)' <-> '$(Short $texts[$b].Text 24)' ($o)") }
            }
            foreach ($bl in $blocks) {
                $o = Overlap $texts[$a].Rect $bl.Rect
                if ($o) { $issues.Add("slide ${n}: TEXT ON $($bl.Kind.ToUpper()) '$(Short $texts[$a].Text 24)' <-> '$($bl.Name)' ($o)") }
            }
        }
        for ($a = 0; $a -lt $blocks.Count; $a++) { for ($b = $a + 1; $b -lt $blocks.Count; $b++) {
            $o = Overlap $blocks[$a].Rect $blocks[$b].Rect
            if ($o -and -not ($cover -and ($blocks[$a].Kind -eq "picture" -and $blocks[$b].Kind -eq "picture"))) { $issues.Add("slide ${n}: $($blocks[$a].Kind.ToUpper()) '$($blocks[$a].Name)' overlaps $($blocks[$b].Kind) '$($blocks[$b].Name)' ($o)") }
        } }
        "slide ${n}: $($texts.Count) text boxes, $($blocks.Count) pictures/charts/tables"
    }
} finally {
    $pres.Close(); if ($app.Presentations.Count -eq 0) { $app.Quit() }
}
if ($issues.Count) { "ISSUES ($($issues.Count)):"; $issues } else { "NO OVERFLOW, OVERLAP, OFF-SLIDE, MARGIN, FOOTER OR CHART-LABEL ISSUES" }
