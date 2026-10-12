local function label(text, size, color, weight)
    return ui.label({text = text, fontSize = size or 12, color = color or "on_surface", maxLines = 1, fontWeight = weight or "normal"})
end
local resetTime = require("./reset-time.luau")
local function resetText(value)
    local epoch = resetTime.parse(value)
    if not epoch then return "Reset not reported" end
    return noctalia.formatTime("%m/%d %H:%M", epoch) .. " · " .. resetTime.relative(epoch, os.time())
end
local names = {five_hour = "5 hours", seven_day = "7 days", primary = "Primary", secondary = "Weekly"}
local function palette()
    local dark = noctalia.isDarkMode()
    return {
        codex = dark and "#9CDDBB" or "#23794F",
        claude = dark and "#EAB69B" or "#984D2D",
        warning = dark and "#F0CA80" or "#886013",
    }
end
local function badge(icon, text, color)
    return ui.row({align = "center", gap = 6}, {
        ui.glyph({name = icon, size = 13, color = color}), label(text, 11, color),
    })
end
local function metric(icon, text, color)
    return ui.row({align = "center", gap = 9, padding = 11, flexGrow = 1, fill = "surface_variant", radius = 10}, {
        ui.glyph({name = icon, size = 18, color = color}), label(text, 13, color, "semibold"),
    })
end
local function quotaCell(w, accent)
    local remaining = tonumber(w.remaining_percent)
    local tone = remaining and remaining <= 10 and "error" or (remaining and remaining <= 20 and palette().warning or accent)
    return ui.column({gap = 4, flexGrow = 1}, {
        ui.row({justify = "space_between", gap = 8}, {
            badge((w.id == "seven_day" or w.id == "secondary") and "calendar" or "hourglass", names[w.id] or w.id or "Quota", "on_surface_variant"),
            label(remaining and string.format("%.0f%% left", remaining) or "Unknown", 13, tone, "semibold"),
        }),
        ui.progress({progress = remaining and math.clamp(remaining / 100, 0, 1) or 0, height = 4, radius = 2, fill = tone, track = "outline/0.35"}),
        badge("clock", resetText(w.reset_at), "on_surface_variant"),
    })
end
local function tierLabel(account)
    local overrides = noctalia.getConfig("tier_labels") or {}
    local key = (account.provider or "") .. ":" .. (account.email or "")
    if overrides[key] and overrides[key] ~= "" then return overrides[key] end
    local tiers = noctalia.state.get("tiers") or {}
    if tiers[tostring(account.auth_index)] then return tiers[tostring(account.auth_index)] end
    local plan = account.plan or account.subscription_type or account.tier
    if account.provider == "codex" then
        if plan == "pro" then return "Pro 200" end
        if plan == "prolite" or plan == "pro-lite" or plan == "pro_lite" or plan == "self_serve_business_prolite" then return "Pro 100" end
    end
    if not plan or plan == "" then return "Tier not reported" end
    local labels = {pro = "Pro", prolite = "Pro Lite", plus = "Plus", free = "Free", team = "Team", business = "Business", enterprise = "Enterprise", max = "Max"}
    return labels[string.lower(tostring(plan))] or tostring(plan)
end
local function render()
    local snapshot = noctalia.state.get("snapshot")
    local err = noctalia.state.get("error") or ""
    local loading = noctalia.state.get("loading") == true
    local content = {
        ui.row({align = "center", gap = 10}, {
            ui.row({padding = 10, fill = "primary/0.15", radius = 13, align = "center"}, {ui.glyph({name = "brain", size = 25, color = "primary"})}),
            ui.column({gap = 2, flexGrow = 1}, {label("AI Proxy", 22, "on_surface", "semibold"), label("All accounts · remaining quota · resets in local time", 12, "on_surface_variant")}),
            ui.button({glyph = "settings", variant = "ghost", onClick = "settings", tooltip = "Plugin settings"}),
            ui.button({glyph = "x", variant = "ghost", onClick = "close"}),
        }),
    }
    if err ~= "" then
        table.insert(content, ui.column({padding = 8, fill = "error/0.12", radius = 8}, {
            ui.label({text = "STALE DATA · " .. err, fontSize = 12, color = "error", maxLines = 2}),
        }))
    end
    if snapshot then
        local s = snapshot.summary
        table.insert(content, ui.row({gap = 8}, {
            metric("circle-check", string.format("%d / %d available", s.available or 0, s.total or 0), palette().codex),
            metric("battery-off", tostring(s.exhausted or 0) .. " exhausted", (s.exhausted or 0) > 0 and "error" or "on_surface_variant"),
            metric("shield-check", tostring(s.errors or 0) .. " scan errors", (s.errors or 0) > 0 and "error" or "on_surface_variant"),
        }))
        local rows, counts, accounts = {}, {}, {}
        for _, a in ipairs(snapshot.accounts or {}) do table.insert(accounts, a) end
        table.sort(accounts, function(a,b)
            local ak = (a.provider or "") .. ":" .. tostring(a.auth_index or a.name or "")
            local bk = (b.provider or "") .. ":" .. tostring(b.auth_index or b.name or "")
            return ak < bk
        end)
        for _, a in ipairs(accounts) do
            local provider = a.provider or "other"
            counts[provider] = (counts[provider] or 0) + 1
            local title = (provider == "codex" and "Codex" or provider == "claude" and "Claude" or provider) .. " " .. tostring(counts[provider])
            if noctalia.getConfig("show_emails") and a.email and a.email ~= "" then title = a.email end
            local status = a.status or "unknown"
            local accent = provider == "claude" and palette().claude or palette().codex
            local icon = provider == "claude" and "sparkles" or "terminal-2"
            local statusIcon = status == "available" and "circle-check" or (status == "exhausted" and "battery-off" or "alert-circle")
            local identity = ui.row({width = 300, gap = 10, align = "center"}, {
                ui.row({padding = 7, fill = "outline/0.35", radius = 10, align = "center"}, {
                    ui.glyph({name = icon, size = 21, color = accent}),
                }),
                ui.column({gap = 5, flexGrow = 1}, {
                    ui.row({gap = 6, align = "center"}, {
                        ui.glyph({name = statusIcon, size = 13, color = status == "available" and accent or "error"}),
                        label(title, 13, "on_surface", "semibold"),
                    }),
                    ui.row({gap = 7, align = "center"}, {
                        label((provider == "codex" and "Codex" or provider == "claude" and "Claude" or provider) .. " · " .. tierLabel(a), 11, accent),
                        label("· " .. status, 11, status == "available" and "on_surface_variant" or "error"),
                    }),
                }),
            })
            local cells = {}
            for _, w in ipairs(a.windows or {}) do table.insert(cells, quotaCell(w, accent)) end
            if #cells == 0 then table.insert(cells, label("No quota windows reported", 12, "on_surface_variant")) end
            table.insert(rows, ui.row({key = tostring(a.auth_index or #rows + 1), align = "center", gap = 18, padding = 9, height = 64, fill = status == "exhausted" and "error/0.08" or "surface_variant", border = status == "exhausted" and "error/0.4" or "outline/0.15", borderWidth = 1, radius = 11}, {
                identity, ui.row({gap = 18, align = "center", flexGrow = 1}, cells),
            }))
        end
        if #rows == 0 then table.insert(rows, label("No accounts reported by this proxy.")) end
        table.insert(content, ui.scroll({flexGrow = 1, gap = 6}, rows))
        local stamp = tostring(snapshot.generated_at or "unknown"):gsub("T", " "):gsub("%.%d+Z$", " UTC"):gsub("Z$", " UTC")
        table.insert(content, badge(err ~= "" and "cloud-off" or "cloud-check", "Snapshot: " .. stamp .. (err ~= "" and " · connection failed" or ""), "on_surface_variant"))
    else
        table.insert(content, ui.column({flexGrow = 1, justify = "center", align = "center", gap = 12}, {
            ui.glyph({name = err ~= "" and "alert-circle" or "loader", size = 32, color = "primary"}),
            label(err ~= "" and "Check the key-file path in settings." or "Fetching account quotas…", 14),
        }))
    end
    table.insert(content, ui.row({gap = 8}, {
        ui.button({text = loading and "Refreshing…" or "Refresh quotas", glyph = "refresh", variant = "primary", enabled = not loading, onClick = "refresh", flexGrow = 1}),
        ui.button({text = "Usage report", glyph = "chart-bar", variant = "outline", onClick = "openUsage"}),
    }))
    panel.render(ui.column({padding = 16, gap = 10, height = 740}, content))
end
local lastMinute = -1
function onOpen(_)
    panel.setWantsSecondTicks(true)
    lastMinute = math.floor(os.time() / 60)
    render()
end
function update()
    local minute = math.floor(os.time() / 60)
    if minute ~= lastMinute then lastMinute = minute; render() end
end
function close() panel.close() end
function settings() noctalia.openSettings() end
function refresh() noctalia.state.set("refresh", noctalia.nowMs()) end
function openUsage()
    local base = (noctalia.getConfig("base_url") or ""):gsub("/+$", "")
    if base:match("^https://[^/%s?#]+$") then noctalia.runAsync({"xdg-open", base .. "/v0/resource/plugins/usage-report/report"}) end
end
noctalia.state.watch("snapshot", render)
noctalia.state.watch("error", render)
noctalia.state.watch("loading", render)

noctalia.state.watch("tiers", render)
