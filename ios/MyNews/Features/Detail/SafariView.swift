import SafariServices
import SwiftUI

/// SwiftUI wrapper for `SFSafariViewController`.
///
/// There is still no native SwiftUI equivalent — iOS 26's `WebView` wraps
/// WebKit, which is the right choice for content you own but the wrong one for
/// third-party article pages. Note App Review requires this controller be
/// visibly presented and never used to track the reader, which is also why the
/// crawler fetches article text server-side rather than through a hidden view.
struct SafariView: UIViewControllerRepresentable {
    let url: URL
    var entersReaderIfAvailable = false

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = entersReaderIfAvailable
        return SFSafariViewController(url: url, configuration: config)
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
