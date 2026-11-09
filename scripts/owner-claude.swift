// Only this fixed bundle is eligible for a graceful restart.
import AppKit
import Foundation

let bundle = "com.anthropic.claudefordesktop"
let instances = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
if CommandLine.arguments.last == "status" {
    print(instances.isEmpty ? "Claude 桌面端未运行。" : "Claude 桌面端正在运行。")
    exit(0)
}
guard CommandLine.arguments.last == "restart", instances.count == 1,
      let app = instances.first, let url = app.bundleURL else {
    print(instances.isEmpty ? "Claude 桌面端未运行，无需重启。" : "未能唯一定位 Claude 桌面端。")
    exit(0)
}
guard app.terminate() else { print("Claude 未接受正常退出请求。"); exit(1) }
let deadline = Date().addingTimeInterval(20)
while !app.isTerminated && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.2))
}
guard app.isTerminated else { print("Claude 尚未退出，请检查是否有待保存的工作。"); exit(1) }
let config = NSWorkspace.OpenConfiguration()
config.activates = false
NSWorkspace.shared.openApplication(at: url, configuration: config) { reopened, error in
    if error == nil, let reopened = reopened, reopened.bundleIdentifier == bundle && !reopened.isTerminated {
        print("Claude 桌面端已正常退出并重新启动。")
        exit(0)
    }
    print("Claude 已退出，重新打开失败。")
    exit(1)
}
RunLoop.current.run()
