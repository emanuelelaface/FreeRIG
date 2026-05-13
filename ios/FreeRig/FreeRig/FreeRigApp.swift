import SwiftUI
import UIKit

@main
struct FreeRIGApp: App {
    @Environment(\.scenePhase) private var scenePhase

    @StateObject private var settings: AppSettings
    @StateObject private var viewModel: RadioViewModel

    init() {
        let settings = AppSettings()
        _settings = StateObject(wrappedValue: settings)
        _viewModel = StateObject(wrappedValue: RadioViewModel(settings: settings))
    }

    var body: some Scene {
        WindowGroup {
            ContentView(viewModel: viewModel, settings: settings)
                .onAppear {
                    updateSystemBehavior(for: scenePhase)
                }
        }
        .onChange(of: scenePhase) { _, phase in
            updateSystemBehavior(for: phase)
            if phase == .background {
                viewModel.releaseAllHeldCommands()
            }
        }
    }

    @MainActor
    private func updateSystemBehavior(for phase: ScenePhase) {
        UIApplication.shared.isIdleTimerDisabled = (phase == .active)
    }
}
