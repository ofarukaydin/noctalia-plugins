local function render()
    local s = noctalia.state.get("snapshot")
    local err = noctalia.state.get("error") or ""
    barWidget.setGlyph("brain")
    if not s then
        barWidget.setText(err ~= "" and "AI !" or "AI …")
        barWidget.setTooltip(err ~= "" and err or "Loading AI proxy quotas…")
        barWidget.setGlyphColor(err ~= "" and "error" or "primary")
        return
    end
    local summary = s.summary
    barWidget.setText(string.format("AI %d/%d%s", summary.available or 0, summary.total or 0, err ~= "" and " !" or ""))
    local tone = err ~= "" and "error" or ((summary.available or 0) == 0 and "error" or "primary")
    barWidget.setGlyphColor(tone)
    barWidget.setTooltip({
        {"AI proxy", err ~= "" and "Last snapshot · connection error" or "Connected"},
        {"Available", tostring(summary.available or 0) .. " / " .. tostring(summary.total or 0)},
        {"Exhausted", tostring(summary.exhausted or 0)},
        {"Scan errors", tostring(summary.errors or 0)},
        {"Click", "Account quotas and reset times"},
        {"Right click", "Refresh upstream quotas"},
    })
end
noctalia.state.watch("snapshot", render)
noctalia.state.watch("error", render)
function update() render() end
function onClick() noctalia.togglePanel("buildpulsar/cpa-quota:panel") end
function onRightClick() noctalia.state.set("refresh", noctalia.nowMs()) end
noctalia.setUpdateInterval(60000)
render()
