-- Shared by the widget, shortcut and panel: read published state, send requests to the service.
local M = {}

function M.devices()
    return noctalia.state.get("devices") or {}
end

function M.summary()
    local total, online, on = 0, 0, 0
    for _, d in ipairs(M.devices()) do
        total = total + 1
        if d.online then online = online + 1 end
        if d.online and d.on then on = on + 1 end
    end
    return {total = total, online = online, on = on}
end

function M.send(request)
    request.n = noctalia.nowMs()
    noctalia.state.set("request", request)
end

function M.setAll(on)
    M.send({ids = {"all"}, on = on})
end

-- Like the app's group switch: anything on turns everything off, otherwise all on.
function M.toggleAll()
    M.setAll(M.summary().on == 0)
end

local PROTOCOL_NAMES = {matter = "Matter", aidot = "AiDot"}

function M.protocolName(protocol)
    return PROTOCOL_NAMES[protocol] or "Unknown"
end

-- "Matter", "AiDot", or "Matter + AiDot" when the lights are split.
function M.protocols()
    local seen, names = {}, {}
    for _, d in ipairs(M.devices()) do
        if d.protocol and not seen[d.protocol] then
            seen[d.protocol] = true
            table.insert(names, M.protocolName(d.protocol))
        end
    end
    table.sort(names, function(a, b) return a > b end)  -- Matter first
    return #names > 0 and table.concat(names, " + ") or nil
end

function M.music()
    return noctalia.state.get("music") or {}
end

function M.setMusic(on, style)
    M.send({music = on, style = style})
end

-- Changes a preference; restarts a running sync so it applies immediately.
function M.setMusicPulse(pulse)
    M.send({music = M.music().active or nil, pulse = pulse})
end

function M.refresh()
    M.send({refresh = true})
end

return M
