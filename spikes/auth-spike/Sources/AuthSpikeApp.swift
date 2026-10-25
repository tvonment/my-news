import SwiftUI
@preconcurrency import MSAL

@main
struct AuthSpikeApp: App {
    @State private var model = SpikeModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                // ASWebAuthenticationSession hands the redirect straight back to MSAL;
                // this only matters if a msauth.com.vonmentlen.mynews:// URL ever
                // arrives from outside (a broker, which CIAM does not use).
                .onOpenURL { url in
                    _ = MSALPublicClientApplication.handleMSALResponse(url, sourceApplication: nil)
                }
        }
    }
}
