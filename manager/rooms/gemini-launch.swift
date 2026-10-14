// Gemini window launch only. No Codex UI control, transcript reads or consent clicks.
import AppKit
import ApplicationServices
import Foundation

struct Job: Decodable {
    let client: String; let app: String; let workspace: String; let prompt: String; let serverName: String
    enum CodingKeys: String, CodingKey { case client,app,workspace,prompt;case serverName="server_name" }
}
enum Failure: String, Error { case permission, input_not_ready, focus_changed, model_busy }
var step = "load"
func attribute(_ node: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(node, name as CFString, &value) == .success ? value : nil
}
func string(_ node: AXUIElement, _ name: String) -> String { attribute(node,name) as? String ?? "" }
func children(_ node: AXUIElement) -> [AXUIElement] { attribute(node,kAXChildrenAttribute) as? [AXUIElement] ?? [] }
func matches(_ text: String, _ patterns: [String]) -> Bool {
    patterns.contains { text.localizedCaseInsensitiveContains($0) }
}
func label(_ node: AXUIElement) -> String {
    [kAXTitleAttribute,kAXDescriptionAttribute,kAXHelpAttribute,kAXIdentifierAttribute].map { string(node,$0) }.joined(separator:" | ")
}
func find(_ root: AXUIElement, roles: [String], test: (AXUIElement) -> Bool) -> AXUIElement? {
    var queue = [root]; var i = 0
    while i < queue.count && i < 6000 {
        let node=queue[i];i += 1
        let role=string(node,kAXRoleAttribute)
        if roles.contains(role) && test(node) { return node }
        // Never traverse the existing conversation body or sidebar history.
        if role == kAXGroupRole && matches(string(node,kAXDescriptionAttribute),["User message","Agent response","Conversation Log"]) { continue }
        queue.append(contentsOf:children(node))
    }
    return nil
}
func wait(_ seconds: Double = 20, _ action: () -> AXUIElement?) -> AXUIElement? {
    let end=Date().addingTimeInterval(seconds)
    repeat { if let found=action() { return found }; Thread.sleep(forTimeInterval:0.2) } while Date() < end
    return nil
}
func launch(_ job: Job) throws {
    if job.client == "probe" {
        guard AXIsProcessTrusted() else { throw Failure.permission }
        return
    }
    guard job.client == "gemini", Bundle(path:job.app)?.bundleIdentifier == "com.google.GeminiMacOS" else { throw Failure.input_not_ready }
    guard AXIsProcessTrusted() else { throw Failure.permission }
    step="open-app"
    let running=NSRunningApplication.runningApplications(withBundleIdentifier:"com.google.GeminiMacOS")
    var app=running.first { $0.bundleURL?.standardizedFileURL.path == URL(fileURLWithPath:job.app).standardizedFileURL.path }
    if app == nil && running.count == 1 { app=running[0] }
    if app == nil {
        let semaphore=DispatchSemaphore(value:0)
        NSWorkspace.shared.openApplication(at:URL(fileURLWithPath:job.app),configuration:NSWorkspace.OpenConfiguration()) { value,_ in app=value;semaphore.signal() }
        guard semaphore.wait(timeout:.now()+15) == .success else { throw Failure.input_not_ready }
    }
    guard let app=app else { throw Failure.input_not_ready }
    app.activate(options:[])
    let root=AXUIElementCreateApplication(app.processIdentifier)
    func guardFocus() throws {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else { throw Failure.focus_changed }
    }
    func button(_ patterns: [String]) -> AXUIElement? {
        find(root,roles:[kAXButtonRole,kAXPopUpButtonRole,kAXMenuItemRole,"AXLink"]) { matches(label($0),patterns) }
    }
    func press(_ node: AXUIElement?) throws {
        try guardFocus()
        guard let node=node else { throw Failure.input_not_ready }
        guard attribute(node,kAXEnabledAttribute) as? Bool != false else { throw Failure.input_not_ready }
        if AXUIElementPerformAction(node,kAXPressAction as CFString) == .success { return }
        guard let rawPosition=attribute(node,kAXPositionAttribute),let rawSize=attribute(node,kAXSizeAttribute),
              CFGetTypeID(rawPosition)==AXValueGetTypeID(),CFGetTypeID(rawSize)==AXValueGetTypeID() else { throw Failure.input_not_ready }
        var point=CGPoint.zero;var size=CGSize.zero
        guard AXValueGetValue(rawPosition as! AXValue,.cgPoint,&point),AXValueGetValue(rawSize as! AXValue,.cgSize,&size),size.width>0,size.height>0 else { throw Failure.input_not_ready }
        point.x += size.width/2;point.y += size.height/2
        guard let down=CGEvent(mouseEventSource:nil,mouseType:.leftMouseDown,mouseCursorPosition:point,mouseButton:.left),
              let up=CGEvent(mouseEventSource:nil,mouseType:.leftMouseUp,mouseCursorPosition:point,mouseButton:.left) else { throw Failure.input_not_ready }
        down.post(tap:.cghidEventTap);up.post(tap:.cghidEventTap)
        Thread.sleep(forTimeInterval:0.15)
    }
    func key(_ code: CGKeyCode, _ flags: CGEventFlags = []) throws {
        try guardFocus()
        guard let down=CGEvent(keyboardEventSource:nil,virtualKey:code,keyDown:true),
              let up=CGEvent(keyboardEventSource:nil,virtualKey:code,keyDown:false) else { throw Failure.input_not_ready }
        down.flags=flags;up.flags=flags;down.post(tap:.cghidEventTap);up.post(tap:.cghidEventTap)
        Thread.sleep(forTimeInterval:0.15)
    }
    func editor() -> AXUIElement? {
        find(root,roles:[kAXComboBoxRole,kAXTextAreaRole,kAXTextFieldRole]) { matches(label($0),["Message input"]) }
    }
    func enter(_ text: String) throws {
        guard let input=wait(20,{ editor() }), string(input,kAXValueAttribute).trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw Failure.input_not_ready }
        try guardFocus()
        AXUIElementSetAttributeValue(input,kAXFocusedAttribute as CFString,kCFBooleanTrue)
        let clipboard=NSPasteboard.general
        let saved=(clipboard.pasteboardItems ?? []).map { item in item.types.compactMap { type -> (NSPasteboard.PasteboardType,Data)? in item.data(forType:type).map { (type,$0) } } }
        clipboard.clearContents();clipboard.setString(text,forType:.string);let count=clipboard.changeCount
        defer {
            if clipboard.changeCount == count {
                clipboard.clearContents()
                let items=saved.map { values -> NSPasteboardItem in let item=NSPasteboardItem();for (type,data) in values { item.setData(data,forType:type) };return item }
                clipboard.writeObjects(items)
            }
        }
        try key(9,.maskCommand)
        guard wait(10,{ guard let node=editor(),string(node,kAXValueAttribute).trimmingCharacters(in:.whitespacesAndNewlines)==text.trimmingCharacters(in:.whitespacesAndNewlines) else { return nil };return node }) != nil else { throw Failure.input_not_ready }
        try press(wait(10,{ guard let node=button(["Send message"]),attribute(node,kAXEnabledAttribute) as? Bool != false else { return nil };return node }))
    }
    step="gemini-mode"
    guard let mode=wait(15,{button(["codeModeSwitcherButton","codeModeExitButton"])}) else { throw Failure.input_not_ready }
    if matches(label(mode),["codeModeSwitcherButton"]) { try press(mode) }
    guard wait(15,{button(["codeModeExitButton"])}) != nil else { throw Failure.input_not_ready }
    if button(["Cancel (","Stop generating"]) != nil { throw Failure.model_busy }
    if let input=editor(),!string(input,kAXValueAttribute).trimmingCharacters(in:.whitespacesAndNewlines).isEmpty { throw Failure.focus_changed }
    step="gemini-new-chat";try press(button(["New Conversation"]))
    step="gemini-project";try press(wait(15,{button(["Select project"])}))
    // New Project is exposed as static text inside an actionable menu row.
    guard let create=wait(10,{find(root,roles:[kAXMenuItemRole,kAXButtonRole,kAXStaticTextRole]) { matches(label($0),["New Project"]) || string($0,kAXValueAttribute)=="New Project" }}) else { throw Failure.input_not_ready }
    try press(create)
    step="gemini-folder"
    guard wait(10,{button(["Открыть","Open"])}) != nil else { throw Failure.input_not_ready }
    try key(5,[.maskCommand,.maskShift])
    guard let path=wait(10,{find(root,roles:[kAXTextFieldRole]) { string($0,kAXIdentifierAttribute)=="PathTextField" }}) else { throw Failure.input_not_ready }
    AXUIElementSetAttributeValue(path,kAXValueAttribute as CFString,job.workspace as CFString)
    try key(36)
    try press(wait(10,{button(["Открыть","Open"]) }))
    guard wait(20,{editor()}) != nil else { throw Failure.input_not_ready }
    step="gemini-warmup"
    try enter("UZEL preparation: call only room_status({}) of MCP server \(job.serverName) if available, then finish. Do not use any other server. You may read only its tool schemas. Do not read user files, run commands or post room messages.")
    guard wait(10,{button(["Cancel ("])}) != nil else { throw Failure.model_busy }
    guard wait(60,{button(["Cancel ("]) == nil ? button(["New Conversation"]) : nil}) != nil else { throw Failure.model_busy }
    try press(button(["New Conversation"]))
    step="gemini-participate";try enter(job.prompt)
}
var result: [String:Any]
do {
    let data=FileHandle.standardInput.readDataToEndOfFile()
    let job=try JSONDecoder().decode(Job.self,from:data)
    try launch(job);result=["ok":true,"step":step]
} catch let error as Failure { result=["ok":false,"step":step,"code":error.rawValue] }
catch { result=["ok":false,"step":step,"code":"input_not_ready"] }
let data=(try? JSONSerialization.data(withJSONObject:result)) ?? Data("{}".utf8)
FileHandle.standardOutput.write(data)
