$outFile  = "D:\work\kaoyanfuzhu\source\03-OCR-full.txt"
$log      = "D:\work\kaoyanfuzhu\docs\autoshutdown.log"
$deadline = (Get-Date).AddMinutes(75)
"[$((Get-Date).ToString('HH:mm:ss'))] watchdog started, waiting for OCR output" | Out-File $log -Encoding utf8
while (-not (Test-Path $outFile)) {
    if ((Get-Date) -gt $deadline) {
        "[$((Get-Date).ToString('HH:mm:ss'))] TIMEOUT after 75min, shutting down anyway" | Out-File $log -Append -Encoding utf8
        break
    }
    Start-Sleep -Seconds 30
}
if (Test-Path $outFile) {
    "[$((Get-Date).ToString('HH:mm:ss'))] OCR output detected, size $((Get-Item $outFile).Length) bytes" | Out-File $log -Append -Encoding utf8
}
Start-Sleep -Seconds 30
"[$((Get-Date).ToString('HH:mm:ss'))] issuing shutdown (90s grace)" | Out-File $log -Append -Encoding utf8
shutdown /s /t 90 /c "OCR finished - auto shutdown"
