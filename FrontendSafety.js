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

function brokerQueueLimitError(lineBytes, queuedLines, queuedBytes,
                               maxLineBytes, maxLines, maxBytes) {
  if (lineBytes > maxLineBytes)
    return "Snapshot backend response exceeded its line limit."
  if (queuedLines >= maxLines)
    return "Snapshot backend parser queue exceeded its line limit."
  if (queuedBytes + lineBytes > maxBytes)
    return "Snapshot backend parser queue exceeded its byte limit."
  return ""
}

function brokerEnvelopeError(message, protocolVersion,
                             maxRequestIdBytes, maxGenerationIdBytes) {
  if (!message || message.protocolVersion !== protocolVersion)
    return "Unsupported snapshot backend protocol."
  if (typeof message.requestId !== "string" || message.requestId === ""
      || utf8Bytes(message.requestId, maxRequestIdBytes + 1) > maxRequestIdBytes)
    return "Invalid snapshot backend request identity."
  if (message.generationId !== undefined
      && (typeof message.generationId !== "string"
          || utf8Bytes(message.generationId, maxGenerationIdBytes + 1)
            > maxGenerationIdBytes))
    return "Invalid snapshot backend generation identity."
  return ""
}

function brokerDirectoryRowError(row, maxPathBytes) {
  if (!row || typeof row.path !== "string" || row.path === ""
      || typeof row.name !== "string" || row.name === ""
      || (row.parent_path !== null && typeof row.parent_path !== "string")
      || utf8Bytes(row.path, maxPathBytes + 1) > maxPathBytes
      || utf8Bytes(row.name, maxPathBytes + 1) > maxPathBytes
      || (row.parent_path !== null
          && utf8Bytes(row.parent_path, maxPathBytes + 1) > maxPathBytes))
    return "Invalid or oversized snapshot item identity."
  if (row.kind !== undefined && row.kind !== "directory" && row.kind !== "file")
    return "Invalid snapshot item type."
  var fields = ["allocated_bytes", "direct_files_bytes", "child_count", "warning_count"]
  for (var index = 0; index < fields.length; index++) {
    var value = row[fields[index]]
    if (typeof value !== "number" || !isFinite(value)
        || value < 0 || Math.floor(value) !== value)
      return "Invalid snapshot item accounting."
  }
  return ""
}

function summonMountpoint(payloadJson, maxPathBytes) {
  try {
    var payload = JSON.parse(String(payloadJson || "{}"))
    if (!payload || payload.mountpoint === undefined) return ""
    if (typeof payload.mountpoint !== "string" || payload.mountpoint === ""
        || payload.mountpoint.charAt(0) !== "/"
        || utf8Bytes(payload.mountpoint, maxPathBytes + 1) > maxPathBytes)
      return ""
    return payload.mountpoint
  } catch (error) {
    return ""
  }
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
