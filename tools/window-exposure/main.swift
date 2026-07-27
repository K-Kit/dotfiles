// window-exposure - report which apps currently have a visible (unoccluded) window.
//
// Reads the on-screen window list front-to-back and, for each layer-0 window,
// decides whether some window IN FRONT of it fully contains it. This is
// Chromium's conservative occlusion fallback (web_contents_occlusion_checker_mac.mm):
// a window counts as occluded only when a SINGLE higher window covers its whole
// frame. Two windows that jointly cover a third are not detected — deliberately,
// because exact rectangle-union geometry is unreliable under transparency and
// rounded corners, and erring towards "visible" is the safe direction here.
//
// Output: one "<pid>\t<0|1>" line per PID that owns at least one candidate window,
// 1 = at least one window exposed. Apps with no candidate window are simply absent
// from the output — that is "unknown", not "not exposed", and the caller must never
// treat absence as grounds to hide.
//
// Exit codes: 0 ok · 1 no usable window list (caller: hide nothing) · 2 screen locked.
//
// Uses only required window-list keys (bounds, layer, alpha, owner PID), which are
// populated without Screen Recording permission. Owner name and window title are
// optional keys whose availability is permission-dependent — never used here.

import CoreGraphics
import Foundation

// A window must be at least this large to count as a real, user-visible window.
// Drops tooltips, shadows, and stray tracking windows.
let minCandidateArea: Double = 5000

// A window must be at least this opaque to hide what is beneath it.
let opaqueAlpha: Double = 0.95

struct Win {
    let pid: pid_t
    let alpha: Double
    let rect: CGRect

    var area: Double { rect.width * rect.height }

    // True if `other` lies entirely inside this window's frame.
    func covers(_ other: Win) -> Bool {
        rect.minX <= other.rect.minX
            && rect.minY <= other.rect.minY
            && rect.maxX >= other.rect.maxX
            && rect.maxY >= other.rect.maxY
    }
}

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("window-exposure: \(message)\n".utf8))
    exit(code)
}

// A locked screen says nothing about what the user can see.
if let session = CGSessionCopyCurrentDictionary() as? [String: Any],
   let locked = session["CGSSessionScreenIsLocked"] as? Int, locked == 1 {
    fail("screen locked", 2)
}

guard let raw = CGWindowListCopyWindowInfo(
    [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
) as? [[String: Any]], !raw.isEmpty else {
    fail("no window list (NULL or empty)", 1)
}

// Layer-0 windows only, front-to-back order preserved. Higher layers are menus,
// the Dock, and overlays: neither tracked as app windows nor trusted as occluders.
var windows: [Win] = []
for info in raw {
    guard let layer = info[kCGWindowLayer as String] as? Int, layer == 0,
          let rawPID = info[kCGWindowOwnerPID as String] as? Int,
          let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
          let rect = CGRect(dictionaryRepresentation: boundsDict as CFDictionary)
    else { continue }
    let pid = pid_t(rawPID)
    let alpha = (info[kCGWindowAlpha as String] as? Double) ?? 1.0
    windows.append(Win(pid: pid, alpha: alpha, rect: rect))
}

var exposedByPID: [pid_t: Bool] = [:]
for (i, candidate) in windows.enumerated() where candidate.area >= minCandidateArea {
    var occluded = false
    for j in 0..<i {
        let front = windows[j]
        if front.alpha >= opaqueAlpha, front.pid != candidate.pid, front.covers(candidate) {
            occluded = true
            break
        }
    }
    exposedByPID[candidate.pid] = (exposedByPID[candidate.pid] ?? false) || !occluded
}

// No candidate windows at all on a non-empty list means we are looking at something
// other than an ordinary desktop (Mission Control, a Space transition). Unknown, not empty.
if exposedByPID.isEmpty {
    fail("no layer-0 candidate windows", 1)
}

var out = ""
for pid in exposedByPID.keys.sorted() {
    out += "\(pid)\t\(exposedByPID[pid]! ? 1 : 0)\n"
}
FileHandle.standardOutput.write(Data(out.utf8))
