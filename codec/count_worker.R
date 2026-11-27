# Fixed ST-COMPARISON encoder. Resource isolation/affinity are inherited from
# the reviewed supervisor. The ordinary existing worker remains the decoder.
args <- commandArgs(trailingOnly=TRUE)
if (length(args) != 6L || args[1] != "encode" || !(args[4] %in% c("col", "row"))) {
  stop("Expected encode input.mtx output.bp col|row scratch receipt.json")
}
if (file.exists(args[3]) || file.exists(args[6])) stop("Outputs must be new")
suppressPackageStartupMessages(library(Matrix))
suppressPackageStartupMessages(library(BPCells))
start <- proc.time()[["elapsed"]]
x <- as(Matrix::readMM(args[2]), "CsparseMatrix")
x <- BPCells::convert_matrix_type(as(x, "IterableMatrix"), "uint32_t")
if (BPCells::storage_order(x) != "col") stop("Expected column-major MM input")
input_seconds <- proc.time()[["elapsed"]] - start
start <- proc.time()[["elapsed"]]
if (args[4] == "col") {
  y <- BPCells::write_matrix_dir(x, args[3], compress=TRUE, overwrite=FALSE)
} else {
  # This changes physical traversal without swapping the logical axes.
  # https://bnprks.github.io/BPCells/reference/transpose_storage_order.html
  y <- BPCells::transpose_storage_order(x, outdir=args[3], tmpdir=args[5],
                                       load_bytes=4194304L, sort_bytes=67108864L)
}
write_seconds <- proc.time()[["elapsed"]] - start
y <- BPCells::open_matrix_dir(args[3])
if (BPCells::storage_order(y) != args[4] || BPCells::matrix_type(y) != "uint32_t" ||
    any(dim(y) != dim(x))) stop("Encoded layout, type or shape differs")
# Fixed scalar schema avoids a new JSON package dependency.
receipt <- sprintf(paste0('{"schema":"st-count-r-1","BPCells_version":"%s",',
                          '"physical_storage_order":"%s","matrix_type":"%s",',
                          '"logical_shape":[%d,%d],"read_convert_seconds":%.9f,',
                          '"write_seconds":%.9f}\n'),
                   as.character(packageVersion("BPCells")), BPCells::storage_order(y),
                   BPCells::matrix_type(y), nrow(y), ncol(y), input_seconds, write_seconds)
connection <- file(args[6], open="wb")
writeChar(receipt, connection, eos=NULL, useBytes=TRUE)
close(connection)
