.pragma library

function utf8Bytes(value, invalidSize) {
  try { return unescape(encodeURIComponent(String(value || ""))).length }
  catch (error) { return invalidSize }
}

function queueLimitError(lineBytes, queuedLines, queuedBytes,
                         maxLineBytes, maxLines, maxBytes) {
  if (lineBytes > maxLineBytes)
    return "Resource limit exceeded: scan event bytes (maximum 65536)"
  if (queuedLines >= maxLines)
    return "Resource limit exceeded: queued scan lines (maximum 4096)"
  if (queuedBytes + lineBytes > maxBytes)
    return "Resource limit exceeded: queued scan bytes (maximum 16777216)"
  return ""
}

function outputLimitExceeded(chunkBytes, retainedBytes, maxBytes) {
  return chunkBytes > maxBytes || retainedBytes + chunkBytes > maxBytes
}

function directoryLimitError(message, directoryCount,
                             maxDirectories, maxPathBytes) {
  if (directoryCount >= maxDirectories)
    return "Resource limit exceeded: staged directories (maximum 500000)"
  if (typeof message.path !== "string"
      || (message.parentPath !== null && typeof message.parentPath !== "string")
      || typeof message.name !== "string"
      || utf8Bytes(message.path, maxPathBytes + 1) > maxPathBytes
      || (message.parentPath !== null
          && utf8Bytes(message.parentPath, maxPathBytes + 1) > maxPathBytes)
      || utf8Bytes(message.name, maxPathBytes + 1) > maxPathBytes)
    return "Scanner returned an invalid or oversized directory path."

  var fields = ["bytes", "directFilesBytes", "childDirectoryCount", "warningCount"]
  for (var index = 0; index < fields.length; index++) {
    var value = message[fields[index]]
    if (typeof value !== "number" || !isFinite(value)
        || value < 0 || Math.floor(value) !== value)
      return "Scanner returned invalid directory accounting data."
  }
  return ""
}
