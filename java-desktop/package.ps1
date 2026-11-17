param(
    [Parameter(Mandatory)][string] $Jdk,
    [Parameter(Mandatory)][string] $Runtime,
    [Parameter(Mandatory)][string] $OutputDir
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$jdkPath = (Resolve-Path -LiteralPath $Jdk).Path
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$target = [IO.Path]::GetFullPath($OutputDir)
$zip = $target + '.zip'
if ((Test-Path -LiteralPath $target) -or (Test-Path -LiteralPath $zip)) {
    throw 'Choose a new output directory and ZIP path.'
}
foreach ($inputRoot in @($source, $jdkPath, $runtimePath)) {
    $prefix = [IO.Path]::TrimEndingDirectorySeparator($inputRoot) + [IO.Path]::DirectorySeparatorChar
    if ($target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Choose an output directory outside the sources and runtimes.'
    }
}
foreach ($file in @(
    "$jdkPath/bin/javac.exe", "$jdkPath/bin/javaw.exe", "$jdkPath/lib/modules",
    "$runtimePath/python/python.exe", "$runtimePath/app/desktop_worker.py",
    "$runtimePath/app/bin/slide_jxl091.exe"
)) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing runtime file $file" }
}

function Copy-RuntimeTree($From, $To, [string[]] $Extra = @()) {
    & robocopy $From $To /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP @Extra
    if ($LASTEXITCODE -ge 8) { throw "Runtime copy failed ($LASTEXITCODE)" }
}

New-Item -ItemType Directory -Path $target | Out-Null
Copy-RuntimeTree $jdkPath "$target/jre"
Copy-RuntimeTree $runtimePath "$target/runtime" @('/XD', '__pycache__', '/XF', 'STPyramid.exe', 'README-EN.txt', '使用说明.txt', '*.pyc')
foreach ($path in @('codec/archive_image_encode.py', 'codec/integrated_encode.py', 'codec/joint_next.py', 'native_tile_encoder.py', 'pyramid_image_archive.py', 'desktop_worker.py')) {
    Copy-Item -LiteralPath (Join-Path $source $path) -Destination "$target/runtime/app/$path"
}
$python = "$target/runtime/python/python.exe"
$app = "$target/runtime/app"
& $python -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from fetch_count_runtime import count_source_ready; sys.exit(0 if count_source_ready() else 1)' $app
if ($LASTEXITCODE -ne 0) { throw 'Install and verify the pinned Count component in the input runtime before packaging.' }
Set-Content -LiteralPath "$target/runtime/OFFLINE" -Encoding ascii -Value 'Use the pinned runtime included in this package.'
Copy-Item -LiteralPath "$PSScriptRoot/Launch-STPyramidJava.cmd", "$PSScriptRoot/README.md", "$source/THIRD_PARTY.md" -Destination $target
$jar = "$target/STPyramidJava-0.2.2-windows.jar"
& "$PSScriptRoot/build.ps1" -Jdk $jdkPath -Output $jar
& "$target/jre/bin/java.exe" -jar $jar help
if ($LASTEXITCODE -ne 0) { throw 'Bundled JAR launch failed.' }
$revision = & git -C $source rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not read the source revision.' }
if (& git -C $source status --porcelain --untracked-files=no) { $revision += ' (working tree modified)' }
Set-Content -LiteralPath "$target/SOURCE_REVISION.txt" -Value $revision -Encoding ascii
[IO.Compression.ZipFile]::CreateFromDirectory($target, "$zip.partial", [IO.Compression.CompressionLevel]::Fastest, $true)
Move-Item -LiteralPath "$zip.partial" -Destination $zip
$digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$zip.sha256" -Value "$digest  $([IO.Path]::GetFileName($zip))" -Encoding ascii
Write-Output $zip
