# Robust resumable downloader for MiniMax H3 weights via hf-mirror.com
$ErrorActionPreference = 'Continue'
$models = 'E:\SD-WEBUI-AKI-V5.0\ComfyUI_windows_portable\ComfyUI\models'
$base   = 'https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main'
$log    = Join-Path $PSScriptRoot 'dl_h3.log'

$files = @(
  @{ rel='loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors';        size=1940000000 },
  @{ rel='text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors';                  size=15530000000 },
  @{ rel='diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors';           size=20800000000 }
)

function Log($m) { "$(Get-Date -Format 'HH:mm:ss') $m" | Add-Content $log; Write-Host $m }

# clean stale hf locks left by the killed snapshot_download
Get-ChildItem "$models\.cache" -Recurse -Filter '*.lock' -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

foreach ($f in $files) {
  $dst = Join-Path $models ($f.rel -replace '/', '\')
  $dir = Split-Path $dst
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  if ((Test-Path $dst) -and ((Get-Item $dst).Length -gt $f.size)) { Log "SKIP (already present): $($f.rel)"; continue }
  $url = "$base/$($f.rel)"
  for ($try = 1; $try -le 30; $try++) {
    Log "TRY ${try}: $($f.rel)"
    # -C - : resume; --speed-limit/--speed-time: abort if stalled <50KB/s for 60s so retry can recover
    & curl.exe -L -sS -C - --fail --retry 3 --retry-delay 5 --speed-limit 51200 --speed-time 60 `
        --connect-timeout 20 -o $dst $url 2>>$log
    $code = $LASTEXITCODE
    if (Test-Path $dst) { $len = (Get-Item $dst).Length } else { $len = 0 }
    Log "  curl exit=$code size=$len / $($f.size)"
    if ($len -ge $f.size) { Log "DONE: $($f.rel)"; break }
    if ($code -eq 33) { & curl.exe -L -sS --fail --retry 3 --retry-delay 5 --speed-limit 51200 --speed-time 60 --connect-timeout 20 -o $dst $url 2>>$log; continue }
    Start-Sleep -Seconds 10
  }
}
Log 'ALL DOWNLOADS FINISHED'
