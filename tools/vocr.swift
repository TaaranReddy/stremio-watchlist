// Apple Vision OCR: vocr <image>... → one line per recognised text block, per image separated by "---".
// Far better than tesseract on stylised reel overlays and posters.
import Foundation
import Vision
import AppKit

for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { print("---"); continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    for o in (req.results ?? []) {
        if let t = o.topCandidates(1).first, t.confidence > 0.3 { print(t.string) }
    }
    print("---")
}
