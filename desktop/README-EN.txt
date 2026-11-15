STPyramid Desktop for Windows x64

1. Extract the entire ZIP into a writable folder. Open STPyramid.exe.
   The interface starts in English. Use the top-right menu to switch to Simplified Chinese.
2. Under Compress & archive, select a mode.
   Count + image takes a tiled pyramidal TIFF and an H5AD with spatial coordinates.
   A JSON metadata attachment is optional. Image only (fast fused) takes the TIFF alone.
3. Choose an output folder and an archive name, then add the job to the queue.
   Start queue runs the jobs in order. Each verified archive is a sample-name.stpyramid folder.
4. Under Verify & restore, select the archive folder containing manifest.json.
   A full integrated restore writes the native TIFF levels, the H5AD and the saved attachments.

The first Count job downloads the pinned Shared_local source from GitHub and checks every
file's size and SHA-256. Later Count jobs reuse the downloaded files. Image-only jobs work offline.
The extraction folder and job output folder must be writable. Intermediate files require extra disk space.
Successful jobs clean their temporary directory unless you select Keep intermediate files after success.
The log shows the retained temporary path if an encoding job fails after work has started.
Choose a fresh output name for each job. Source TIFF, H5AD and JSON files stay in place.

Older archives may pin a different decoder build. In Verify & restore, choose the matching
decoder runtime folder containing the 089, 091 and hf subfolders.
