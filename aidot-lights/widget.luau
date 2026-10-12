local lights = require("./lights.luau")

local function render()
    local s = lights.summary()
    local err = noctalia.state.get("error") or ""
    local music = lights.music()
    barWidget.setText("")
    if music.active then
        barWidget.setGlyph("music")
        barWidget.setGlyphColor(music.rgb or "primary")
    else
        barWidget.setGlyph(s.on > 0 and "bulb" or "bulb-off")
        barWidget.setGlyphColor(err ~= "" and s.online == 0 and "error" or (s.on > 0 and "primary" or "on_surface"))
    end

    local rows = {}
    for _, d in ipairs(lights.devices()) do
        local value = "Offline"
        if d.online then value = d.on and string.format("On · %d%%", d.brightness or 0) or "Off" end
        table.insert(rows, {d.name, value})
    end
    if music.active then
        table.insert(rows, 1, {"Music sync", (music.style == "smooth" and "Smooth" or "Beat") .. " · on"})
    end
    if #rows == 0 then table.insert(rows, {"AiDot", err ~= "" and err or "Loading…"}) end
    local protocols = lights.protocols()
    if protocols then table.insert(rows, {"Protocol", protocols}) end
    table.insert(rows, {"Click", "Light controls"})
    table.insert(rows, {"Right click", s.on > 0 and "Turn all off" or "Turn all on"})
    table.insert(rows, {"Scroll", "Dim lights that are on"})
    barWidget.setTooltip(rows)
end

function onClick() noctalia.togglePanel("buildpulsar/aidot-lights:panel") end
function onRightClick() lights.toggleAll() end

function onScroll(axis, steps)
    if axis ~= "vertical" or lights.music().active then return end
    local ids, level = {}, nil
    for _, d in ipairs(lights.devices()) do
        if d.online and d.on then
            table.insert(ids, d.id)
            level = level or d.brightness
        end
    end
    if #ids == 0 then return end
    lights.send({ids = ids, brightness = math.clamp((level or 100) - steps * 10, 1, 100)})
end

noctalia.state.watch("devices", render)
noctalia.state.watch("error", render)
noctalia.state.watch("music", render)
render()
