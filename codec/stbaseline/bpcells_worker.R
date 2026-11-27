args <- commandArgs(trailingOnly=TRUE)
suppressPackageStartupMessages(library(Matrix))
suppressPackageStartupMessages(library(BPCells))
if (args[1] == "encode") {
  x <- as(Matrix::readMM(args[2]), "CsparseMatrix")
  x <- BPCells::convert_matrix_type(as(x, "IterableMatrix"), "uint32_t")
  BPCells::write_matrix_dir(x, args[3], compress=TRUE, overwrite=FALSE)
} else if (args[1] == "serve") {
  x <- BPCells::open_matrix_dir(args[2])
  channel <- socketConnection(host="127.0.0.1", port=as.integer(args[3]),
                              server=FALSE, blocking=TRUE, open="r+b", timeout=120)
  writeBin(charToRaw("BPC1"), channel)
  repeat {
    line <- readLines(channel, n=1, warn=FALSE)
    if (!length(line) || line == "QUIT") break
    tryCatch({
      fields <- strsplit(line, "\t", fixed=TRUE)[[1]]
      ri <- as.integer(strsplit(fields[1], ",", fixed=TRUE)[[1]]) + 1L
      gi <- if (fields[2] == "*") seq_len(nrow(x)) else as.integer(strsplit(fields[2], ",", fixed=TRUE)[[1]]) + 1L
      start <- proc.time()[["elapsed"]]
      answer <- as.matrix(x[gi, ri, drop=FALSE])
      elapsed <- proc.time()[["elapsed"]] - start
      values <- as.double(answer)
      # uint32 Counts are exactly representable in IEEE float64, without text formatting.
      writeBin(c(elapsed, length(values), values), channel, size=8, endian="little")
    }, error=function(e) {
      bytes <- charToRaw(conditionMessage(e))
      writeBin(c(-1, length(bytes)), channel, size=8, endian="little")
      writeBin(bytes, channel)
    })
  }
  close(channel)
} else stop("Unknown operation")
