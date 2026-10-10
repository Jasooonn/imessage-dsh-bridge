import Darwin
import Foundation

enum HelperError: Error {
    case invalidArguments
    case sourceOutsideMessages
    case destinationOutsideBridge
    case sourceNotRegular
    case sourceTooLarge
    case destinationExists
}

let arguments = CommandLine.arguments
guard arguments.count == 4, let maxBytes = UInt64(arguments[3]) else {
    fputs("invalid arguments\n", stderr)
    exit(2)
}

let sourceInput = URL(fileURLWithPath: arguments[1])
let destination = URL(fileURLWithPath: arguments[2]).standardizedFileURL
let messagesRoot = URL(
    fileURLWithPath: NSHomeDirectory() + "/Library/Messages/Attachments",
    isDirectory: true
).standardizedFileURL
let bridgeRoot = URL(
    fileURLWithPath: NSHomeDirectory() + "/Library/Application Support/iMessageDSHarness/inbox",
    isDirectory: true
).standardizedFileURL

func isDescendant(_ child: URL, of root: URL) -> Bool {
    let rootPath = root.path.hasSuffix("/") ? root.path : root.path + "/"
    return child.path.hasPrefix(rootPath)
}

do {
    let source = sourceInput.resolvingSymlinksInPath().standardizedFileURL
    guard isDescendant(source, of: messagesRoot) else {
        throw HelperError.sourceOutsideMessages
    }
    guard isDescendant(destination, of: bridgeRoot) else {
        throw HelperError.destinationOutsideBridge
    }

    var info = stat()
    guard lstat(sourceInput.path, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG else {
        throw HelperError.sourceNotRegular
    }
    guard UInt64(info.st_size) <= maxBytes else {
        throw HelperError.sourceTooLarge
    }
    guard !FileManager.default.fileExists(atPath: destination.path) else {
        throw HelperError.destinationExists
    }

    try FileManager.default.copyItem(at: source, to: destination)
    guard chmod(destination.path, S_IRUSR | S_IWUSR) == 0 else {
        try? FileManager.default.removeItem(at: destination)
        throw POSIXError(.EACCES)
    }
    exit(0)
} catch HelperError.sourceOutsideMessages {
    fputs("source is outside Messages attachments\n", stderr)
} catch HelperError.destinationOutsideBridge {
    fputs("destination is outside bridge inbox\n", stderr)
} catch HelperError.sourceNotRegular {
    fputs("source is not a regular file\n", stderr)
} catch HelperError.sourceTooLarge {
    fputs("source exceeds size limit\n", stderr)
} catch HelperError.destinationExists {
    fputs("destination already exists\n", stderr)
} catch {
    fputs("copy denied or failed\n", stderr)
}
exit(1)
