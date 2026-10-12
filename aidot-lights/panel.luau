local lights = require("./lights.luau")

local SWATCHES = {"#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#00c7be", "#007aff", "#5856d6", "#af52de", "#ff2d55"}
local WHITES = {{1800, "Candle"}, {2700, "Warm"}, {4000, "Neutral"}, {5000, "Daylight"}, {6500, "Cool"}}

local function label(text, size, color, weight)
    return ui.label({text = text, fontSize = size or 12, color = color or "on_surface", maxLines = 1, fontWeight = weight or "normal"})
end

-- Approximate black-body colour for a white temperature, for swatches and icons.
local function kelvinTint(k)
    local t = math.clamp((k - 1800) / (6500 - 1800), 0, 1)
    local r = math.floor(255 - 25 * t)
    local g = math.floor(147 + 93 * t)
    local b = math.floor(41 + 214 * t)
    return string.format("#%02x%02x%02x", r, g, b)
end

local function bulbColor(d)
    if not d.online or not d.on then return "on_surface_variant" end
    if d.mode == "color" and d.rgb then return d.rgb end
    if d.cct then return kelvinTint(d.cct) end
    return "primary"
end

local function sliderRow(glyph, glyphColor, slider, valueText)
    return ui.row({align = "center", gap = 10}, {
        ui.glyph({name = glyph, size = 16, color = glyphColor}),
        ui.slider(slider),
        ui.row({width = 52, justify = "end"}, {label(valueText, 12, "on_surface_variant")}),
    })
end

local function card(d)
    local caps = d.caps or {}
    local status = "Offline"
    if d.online then status = d.on and string.format("On · %d%%", d.brightness or 0) or "Off" end
    local header = ui.row({align = "center", gap = 12}, {
        ui.row({padding = 8, radius = 12, fill = d.online and d.on and "primary/0.14" or "outline/0.25", align = "center"}, {
            ui.glyph({name = d.online and d.on and "bulb" or "bulb-off", size = 22, color = bulbColor(d)}),
        }),
        ui.column({gap = 2, flexGrow = 1}, {
            label(d.name or d.id, 14, "on_surface", "semibold"),
            label(status .. " · " .. lights.protocolName(d.protocol) .. (d.ip and (" · " .. d.ip) or ""), 11,
                d.online and "on_surface_variant" or "error"),
        }),
        ui.toggle({
            key = d.id .. ":power", checked = d.online and d.on == true, enabled = d.online == true,
            onChange = function(value) lights.send({ids = {d.id}, on = value == "true"}) end,
        }),
    })

    local body = {header}
    if d.online then
        table.insert(body, sliderRow("sun", "on_surface_variant", {
            key = d.id .. ":brightness", min = 1, max = 100, step = 1, value = d.brightness or 100,
            flexGrow = 1, controlSize = "sm",
            onChange = function(value) lights.send({ids = {d.id}, brightness = tonumber(value)}) end,
        }, string.format("%d%%", d.brightness or 0)))

        local white = d.mode ~= "color" or not caps.rgbw
        if caps.cct and caps.rgbw then
            table.insert(body, ui.row({gap = 6}, {
                ui.button({text = "White", glyph = "sun-high", controlSize = "sm", flexGrow = 1,
                    variant = white and "secondary" or "ghost", selected = white,
                    onClick = function() if not white then lights.send({ids = {d.id}, cct = d.cct or 2700}) end end}),
                ui.button({text = "Colour", glyph = "palette", controlSize = "sm", flexGrow = 1,
                    variant = white and "ghost" or "secondary", selected = not white,
                    onClick = function() if white then lights.send({ids = {d.id}, rgb = d.rgb or "#ff9500"}) end end}),
            }))
        end

        if caps.cct and white then
            local lo, hi = caps.cctMin or 2700, caps.cctMax or 6500
            local cct = math.clamp(d.cct or 2700, lo, hi)
            table.insert(body, sliderRow("temperature", kelvinTint(cct), {
                key = d.id .. ":cct", min = lo, max = hi, step = 50, value = cct,
                flexGrow = 1, controlSize = "sm",
                onChange = function(value) lights.send({ids = {d.id}, cct = tonumber(value)}) end,
            }, string.format("%dK", cct)))
            local chips = {}
            for _, preset in ipairs(WHITES) do
                local k, name = preset[1], preset[2]
                if k >= lo and k <= hi then
                    local selected = math.abs(cct - k) < 50
                    table.insert(chips, ui.column({key = d.id .. ":k" .. k, align = "center", gap = 4, flexGrow = 1,
                        tooltip = string.format("%s · %dK", name, k),
                        onClick = function() lights.send({ids = {d.id}, cct = k}) end}, {
                        ui.box({width = 26, height = 26, radius = 13, fill = kelvinTint(k),
                            border = selected and "on_surface" or "outline/0.4", borderWidth = selected and 2 or 1}),
                        label(name, 10, selected and "on_surface" or "on_surface_variant"),
                    }))
                end
            end
            table.insert(body, ui.row({gap = 4}, chips))
        elseif caps.rgbw then
            local chips = {}
            for _, hex in ipairs(SWATCHES) do
                local selected = d.rgb == hex
                table.insert(chips, ui.box({
                    key = d.id .. ":" .. hex, width = 26, height = 26, radius = 13, fill = hex,
                    border = selected and "on_surface" or "outline/0.4", borderWidth = selected and 2 or 1,
                    onClick = function() lights.send({ids = {d.id}, rgb = hex}) end,
                }))
            end
            table.insert(chips, ui.spacer({flexGrow = 1}))
            table.insert(chips, ui.button({
                glyph = "color-picker", variant = "ghost", controlSize = "sm", tooltip = "Custom colour",
                onClick = function()
                    noctalia.openColorPicker(d.rgb or "#ffffff", function(color)
                        if color ~= nil then lights.send({ids = {d.id}, rgb = color}) end
                    end)
                end,
            }))
            table.insert(body, ui.row({align = "center", gap = 7}, chips))
        end
    elseif d.error then
        table.insert(body, label(d.error, 11, "on_surface_variant"))
    end

    return ui.column({key = d.id, gap = 10, padding = 12, radius = 12, fill = "surface_variant",
        border = "outline/0.15", borderWidth = 1}, body)
end

local function musicCard(online)
    local music = lights.music()
    local on = music.active or music.starting
    local subtitle = "Colours follow whatever this PC is playing"
    if music.starting then subtitle = "Starting…"
    elseif music.active then
        local tempo = music.bpm and string.format("%d BPM, in sync", music.bpm)
            or (music.engine == "aubio" and "finding the beat…" or "following the bass")
        local detail = string.format("%d light%s · %s", music.connected or 0, music.connected == 1 and "" or "s", tempo)
        if music.style == "auto" and music.mode then
            detail = "Auto · " .. music.mode:sub(1, 1):upper() .. music.mode:sub(2) .. " · " .. detail
        end
        if music.section and music.section > 0 then detail = detail .. " · part " .. (music.section + 1) end
        if music.mode == "melody" and music.notes and #music.notes > 0 then
            detail = detail .. " · " .. table.concat(music.notes, " ")
        end
        subtitle = detail .. " · changing a light stops it"
    end
    local rows = {
        ui.row({align = "center", gap = 12}, {
            ui.row({padding = 8, radius = 12, fill = on and "primary/0.14" or "outline/0.25", align = "center"}, {
                ui.glyph({name = "music", size = 22, color = music.active and music.rgb or "on_surface_variant"}),
            }),
            ui.column({gap = 2, flexGrow = 1}, {
                label("Music sync", 14, "on_surface", "semibold"),
                ui.label({text = subtitle, fontSize = 11, color = "on_surface_variant", maxLines = 2}),
            }),
            ui.toggle({key = "music:power", checked = on, enabled = online > 0,
                onChange = function(value) lights.setMusic(value == "true") end}),
        }),
    }
    table.insert(rows, ui.row({align = "center", gap = 10}, {
        ui.glyph({name = "brightness-up", size = 16, color = "on_surface_variant"}),
        ui.column({gap = 1, flexGrow = 1}, {
            label("Pulse brightness", 12, "on_surface"),
            label(music.pulse and "Brightness follows the loudness" or "Lights stay at full brightness", 11, "on_surface_variant"),
        }),
        ui.toggle({key = "music:pulse", checked = music.pulse == true,
            onChange = function(value) lights.setMusicPulse(value == "true") end}),
    }))
    if on then
        local style = music.style or "beat"
        local function styleButton(id, text, glyph, tooltip)
            return ui.button({text = text, glyph = glyph, controlSize = "sm", flexGrow = 1,
                variant = style == id and "secondary" or "ghost", selected = style == id, tooltip = tooltip,
                onClick = function() if style ~= id then lights.setMusic(true, id) end end})
        end
        table.insert(rows, ui.row({gap = 6}, {
            styleButton("beat", "Beat", "wave-square", "Each beat steps to another colour of the section's palette"),
            styleButton("smooth", "Smooth", "wave-sine", "Glide around the palette at the song's tempo, warmer or cooler with the sound"),
            styleButton("melody", "Melody", "music", "Colour per note; with several lights they show the chord"),
            styleButton("auto", "Auto", "wand", "Beat in drops, Smooth in breakdowns and builds, Melody in melodic parts"),
        }))
        table.insert(rows, ui.progress({progress = math.clamp(music.level or 0, 0, 1), height = 4, radius = 2,
            fill = music.rgb or "primary", track = "outline/0.3"}))
    end
    return ui.column({gap = 10, padding = 12, radius = 12, fill = "surface_variant",
        border = on and "primary/0.4" or "outline/0.15", borderWidth = 1}, rows)
end

local function setupCard(err)
    local helper = noctalia.pluginDir() .. "/aidot.py"
    return ui.column({gap = 10, padding = 14, radius = 12, fill = "surface_variant"}, {
        label("Connect your AiDot account", 14, "on_surface", "semibold"),
        ui.label({text = "The AiDot cloud is used once to fetch each bulb's local key. After that the bulbs are controlled directly over your LAN.",
            fontSize = 12, color = "on_surface_variant", maxLines = 4}),
        ui.label({text = err, fontSize = 11, color = "error", maxLines = 3}),
        ui.button({text = "Sign in from a terminal", glyph = "terminal-2", variant = "primary",
            onClick = function()
                noctalia.runInTerminal("python3 -I '" .. helper .. "' login; printf '\\nPress Enter to close'; read _")
            end}),
    })
end

local function render()
    local devices = lights.devices()
    local err = noctalia.state.get("error") or ""
    local busy = noctalia.state.get("busy") == true
    local s = lights.summary()

    local content = {
        ui.row({align = "center", gap = 10}, {
            ui.row({padding = 9, fill = "primary/0.15", radius = 12, align = "center"}, {
                ui.glyph({name = "bulb", size = 22, color = "primary"}),
            }),
            ui.column({gap = 2, flexGrow = 1}, {
                label("Lights", 20, "on_surface", "semibold"),
                label(s.total > 0 and string.format("%d of %d on · %d online · %s", s.on, s.total, s.online, lights.protocols() or "")
                    or "AiDot · local control", 12, "on_surface_variant"),
            }),
            ui.button({glyph = busy and "loader" or "refresh", variant = "ghost", tooltip = "Refresh", onClick = lights.refresh}),
            ui.button({glyph = "settings", variant = "ghost", tooltip = "Plugin settings", onClick = function() noctalia.openSettings() end}),
            ui.button({glyph = "x", variant = "ghost", onClick = function() panel.close() end}),
        }),
    }

    if #devices == 0 then
        if err:find("credentials", 1, true) then
            table.insert(content, setupCard(err))
        else
            table.insert(content, ui.column({flexGrow = 1, justify = "center", align = "center", gap = 10}, {
                ui.glyph({name = err ~= "" and "alert-circle" or "loader", size = 30, color = err ~= "" and "error" or "primary"}),
                ui.label({text = err ~= "" and err or "Looking for lights…", fontSize = 13, color = "on_surface_variant", maxLines = 3}),
            }))
        end
    else
        if err ~= "" then
            table.insert(content, ui.column({padding = 8, fill = "error/0.12", radius = 8}, {
                ui.label({text = err, fontSize = 12, color = "error", maxLines = 2}),
            }))
        end
        table.insert(content, ui.row({gap = 8}, {
            ui.button({text = "All on", glyph = "bulb", variant = "outline", flexGrow = 1, enabled = s.online > 0,
                onClick = function() lights.setAll(true) end}),
            ui.button({text = "All off", glyph = "bulb-off", variant = "outline", flexGrow = 1, enabled = s.online > 0,
                onClick = function() lights.setAll(false) end}),
        }))
        local cards = {musicCard(s.online)}
        for _, d in ipairs(devices) do table.insert(cards, card(d)) end
        table.insert(content, ui.scroll({flexGrow = 1, gap = 10}, cards))
    end

    panel.render(ui.column({padding = 16, gap = 12, height = 620}, content))
end

function onOpen(_)
    lights.refresh()
    render()
end

noctalia.state.watch("devices", render)
noctalia.state.watch("error", render)
noctalia.state.watch("busy", render)
noctalia.state.watch("music", render)
