Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$cache = "c:\Project\Parser\output\glavsnab_checko\cache"
$total = 1357

$form = New-Object System.Windows.Forms.Form
$form.Text = "Проход Главснаб — Checko"
$form.Size = New-Object System.Drawing.Size(560, 360)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font("Segoe UI", 11)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Считаю..."
$title.Font = New-Object System.Drawing.Font("Segoe UI", 28, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $false
$title.Size = New-Object System.Drawing.Size(520, 56)
$title.Location = New-Object System.Drawing.Point(20, 16)

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(20, 84)
$bar.Size = New-Object System.Drawing.Size(500, 28)
$bar.Minimum = 0
$bar.Maximum = $total
$bar.Style = "Continuous"

$detail = New-Object System.Windows.Forms.Label
$detail.AutoSize = $false
$detail.Size = New-Object System.Drawing.Size(500, 100)
$detail.Location = New-Object System.Drawing.Point(20, 128)
$detail.Text = "Считаю компании..."

$foot = New-Object System.Windows.Forms.Label
$foot.AutoSize = $false
$foot.Size = New-Object System.Drawing.Size(500, 48)
$foot.Location = New-Object System.Drawing.Point(20, 240)
$foot.ForeColor = [System.Drawing.Color]::DimGray
$foot.Text = "Локальный проход и два сервера. Окно само обновляется."

$form.Controls.AddRange(@($title, $bar, $detail, $foot))

function Update-Progress {
    $script:reliable = 0
    $script:risky = 0
    $script:nodata = 0
    $script:other = 0
    if (Test-Path $cache) {
        Get-ChildItem $cache -Filter *.json | ForEach-Object {
            $raw = [System.IO.File]::ReadAllText($_.FullName)
            $err = ""
            if ($raw -match '"Ошибка":\s*"([^"]*)"') { $err = $Matches[1].ToLower() }
            if ($err -match "connection closed|timeout|blocked|429|403|target closed|target page|browser has been closed") { return }
            $light = ""
            if ($raw -match '"Светофор":\s*"([^"]*)"') { $light = $Matches[1] }
            switch ($light) {
                "Надежный" { $script:reliable++ }
                "Рискованный" { $script:risky++ }
                "Нет данных" { $script:nodata++ }
                default { if ($light) { $script:other++ } }
            }
        }
    }
    $reliable = $script:reliable
    $risky = $script:risky
    $nodata = $script:nodata
    $other = $script:other
    $done = $reliable + $risky + $nodata + $other
    if ($done -gt $total) { $done = $total }
    $left = $total - $done
    $pct = [math]::Round(100.0 * $done / $total)
    $title.Text = "$done из $total"
    $bar.Value = [Math]::Min($done, $total)
    $detail.Text = "Надёжные: $reliable`r`nРискованные: $risky`r`nНет отчётности: $nodata`r`nОсталось: $left   ($pct%)"
    $foot.Text = "Обновлено " + (Get-Date -Format "HH:mm:ss") + ". Окно само обновляется каждые 8 секунд."
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 4000
$timer.Add_Tick({ Update-Progress })
$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    Update-Progress
    $timer.Start()
})
[void]$form.ShowDialog()
$timer.Stop()
