param(
    [Parameter(Mandatory)][string] $Jdk,
    [Parameter(Mandatory)][string] $Output
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $Jdk 'bin/javac.exe'
if (!(Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'Choose a JDK root containing bin/javac.exe.'
}
$target = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $target) { throw "Choose a new output JAR: $target" }
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
$stage = [IO.Directory]::CreateDirectory((Join-Path ([IO.Path]::GetTempPath()) ('stpyramid-java-' + [guid]::NewGuid())))
try {
    $classes = $stage.CreateSubdirectory('classes').FullName
    $sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'src/stpyramid') -Filter '*.java' | Select-Object -ExpandProperty FullName)
    & $compiler -encoding UTF-8 --release 17 -Xlint:all -d $classes @sources
    if ($LASTEXITCODE -ne 0) { throw "Java compilation failed ($LASTEXITCODE)." }
    $meta = [IO.Directory]::CreateDirectory((Join-Path $classes 'META-INF'))
    [IO.File]::WriteAllLines((Join-Path $meta.FullName 'MANIFEST.MF'),
        @('Manifest-Version: 1.0', 'Main-Class: stpyramid.Main', ''), [Text.Encoding]::ASCII)
    [IO.Compression.ZipFile]::CreateFromDirectory($classes, $target)
    Write-Output $target
} finally {
    $tempRoot = [IO.Path]::TrimEndingDirectorySeparator((Get-Item -LiteralPath ([IO.Path]::GetTempPath())).FullName)
    $found = Get-Item -LiteralPath $stage.FullName
    if ($found.Parent.FullName -ne $tempRoot -or $found.Name -notmatch '^stpyramid-java-[0-9a-f-]+$') {
        throw 'Unexpected compiler staging directory.'
    }
    Remove-Item -LiteralPath $found.FullName -Recurse -Force
}
