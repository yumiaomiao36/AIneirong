import Foundation
import AVFoundation

if CommandLine.arguments.count < 4 {
    fputs("Usage: swift compose_audio_video.swift <video> <audio> <output>\n", stderr)
    exit(2)
}

let videoURL = URL(fileURLWithPath: CommandLine.arguments[1])
let audioURL = URL(fileURLWithPath: CommandLine.arguments[2])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[3])

let composition = AVMutableComposition()
let videoAsset = AVURLAsset(url: videoURL)
let audioAsset = AVURLAsset(url: audioURL)

guard let sourceVideoTrack = videoAsset.tracks(withMediaType: .video).first else {
    fputs("No video track found\n", stderr)
    exit(3)
}

guard let compositionVideoTrack = composition.addMutableTrack(
    withMediaType: .video,
    preferredTrackID: kCMPersistentTrackID_Invalid
) else {
    fputs("Unable to create video track\n", stderr)
    exit(4)
}

let videoDuration = videoAsset.duration
try compositionVideoTrack.insertTimeRange(
    CMTimeRange(start: .zero, duration: videoDuration),
    of: sourceVideoTrack,
    at: .zero
)
compositionVideoTrack.preferredTransform = sourceVideoTrack.preferredTransform

if let sourceAudioTrack = audioAsset.tracks(withMediaType: .audio).first,
   let compositionAudioTrack = composition.addMutableTrack(
        withMediaType: .audio,
        preferredTrackID: kCMPersistentTrackID_Invalid
   ) {
    let audioDuration = audioAsset.duration
    let duration = CMTimeMinimum(videoDuration, audioDuration)
    try compositionAudioTrack.insertTimeRange(
        CMTimeRange(start: .zero, duration: duration),
        of: sourceAudioTrack,
        at: .zero
    )
}

try? FileManager.default.removeItem(at: outputURL)

guard let exporter = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality) else {
    fputs("Unable to create exporter\n", stderr)
    exit(5)
}

exporter.outputURL = outputURL
exporter.outputFileType = .mp4
exporter.shouldOptimizeForNetworkUse = true
exporter.timeRange = CMTimeRange(start: .zero, duration: videoDuration)

let semaphore = DispatchSemaphore(value: 0)
exporter.exportAsynchronously {
    semaphore.signal()
}
semaphore.wait()

if exporter.status != .completed {
    let message = exporter.error?.localizedDescription ?? "Unknown export error"
    fputs(message + "\n", stderr)
    exit(6)
}
