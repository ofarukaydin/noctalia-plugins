local lights = require("./lights.luau")

local function render()
    local s = lights.summary()
    shortcut.setLabel(s.on > 0 and string.format("Lights · %d on", s.on) or "Lights")
    shortcut.setIcon("bulb", "bulb-off")
    shortcut.setActive(s.on > 0)
    shortcut.setEnabled(s.online > 0)
end

function onClick() lights.toggleAll() end
function onRightClick() noctalia.togglePanel("buildpulsar/aidot-lights:panel") end

noctalia.state.watch("devices", render)
render()
