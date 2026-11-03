param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$DestinationPath,
    [ValidateSet("plain", "threshold", "invert")]
    [string]$Mode = "plain"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$src = [System.Drawing.Image]::FromFile($SourcePath)
$scale = 6
$w = [Math]::Max(300, $src.Width * $scale)
$h = [Math]::Max(100, $src.Height * $scale)
$tmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($tmp)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
$g.Clear([System.Drawing.Color]::White)
$g.DrawImage($src, 0, 0, $w, $h)
$src.Dispose()
$g.Dispose()

if ($Mode -eq "plain") {
    $tmp.Save($DestinationPath)
    $tmp.Dispose()
    return
}

$dst = New-Object System.Drawing.Bitmap $w, $h
for ($y = 0; $y -lt $h; $y++) {
    for ($x = 0; $x -lt $w; $x++) {
        $pixel = $tmp.GetPixel($x, $y)
        $gray = [int](0.299 * $pixel.R + 0.587 * $pixel.G + 0.114 * $pixel.B)
        if ($Mode -eq "invert") {
            $gray = 255 - $gray
        }
        $color = if ($gray -lt 170) { [System.Drawing.Color]::Black } else { [System.Drawing.Color]::White }
        $dst.SetPixel($x, $y, $color)
    }
}
$tmp.Dispose()
$dst.Save($DestinationPath)
$dst.Dispose()
