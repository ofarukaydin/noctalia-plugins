local busy = false
local generation = 0
local lastAttempt = 0
local lastForced = 0
local route = "/v0/management/plugins/cpa-quota-api-extension/v1/quotas?limit=1000"

local tierCache = {}
local function enrichTiers(accounts, base, token, force, requestGeneration)
    for _, account in ipairs(accounts) do
        if account.provider == "claude" and account.auth_index then
            local id = tostring(account.auth_index)
            local cached = tierCache[id]
            if not cached or force or os.time() - cached.checked > 1800 then
                tierCache[id] = {checked = os.time(), plan = cached and cached.plan}
                noctalia.http({
                    url = base .. "/v0/management/api-call",
                    method = "POST",
                    headers = {"Content-Type: application/json", "Authorization: Bearer " .. token},
                    follow_redirects = false,
                    body = noctalia.json.encode({
                        authIndex = id, method = "GET",
                        url = "https://api.anthropic.com/api/oauth/profile",
                        header = {Authorization = "Bearer $TOKEN$", ["anthropic-beta"] = "oauth-2025-04-20", ["User-Agent"] = "claude-cli/2.1.280 (external, cli)", ["Content-Type"] = "application/json"},
                    }),
                }, function(response)
                    if generation ~= requestGeneration then return end
                    if not response.ok or response.status ~= 200 then return end
                    local envelope = noctalia.json.decode(response.body)
                    if type(envelope) ~= "table" or envelope.status_code ~= 200 then return end
                    local profile = envelope.body
                    if type(profile) == "string" then profile = noctalia.json.decode(profile) end
                    if type(profile) ~= "table" then return end
                    local org, user = profile.organization or {}, profile.account or {}
                    local plan = nil
                    if org.organization_type == "claude_team" and org.subscription_status == "active" then plan = "Team"
                    elseif user.has_claude_max == true then plan = "Max"
                    elseif user.has_claude_pro == true then plan = "Pro"
                    elseif user.has_claude_max == false and user.has_claude_pro == false then plan = "Free" end
                    if plan then
                        tierCache[id] = {checked = os.time(), plan = plan}
                        local tiers = noctalia.state.get("tiers") or {}
                        tiers[id] = plan
                        noctalia.state.set("tiers", tiers)
                    end
                end)
            end
        end
    end
end

local function publishError(message)
    busy = false
    noctalia.state.set("loading", false)
    noctalia.state.set("error", message)
end

local function refresh(force)
    if busy then return end
    local now = os.time()
    if force and now - lastForced < 30 then return end
    lastAttempt = now
    if force then lastForced = now end
    local base = noctalia.string.trim(noctalia.getConfig("base_url") or ""):gsub("/+$", "")
    if not base:match("^https://[^/%s?#]+$") then
        publishError("Set a valid HTTPS proxy URL in plugin settings.")
        return
    end
    local raw = noctalia.readFile(noctalia.expandPath(noctalia.getConfig("key_file") or ""))
    local token = raw and noctalia.string.trim(raw) or ""
    if token == "" or token:find("[\r\n]") then
        publishError("Management key file is missing or invalid. Check plugin settings.")
        return
    end
    busy = true
    noctalia.state.set("loading", true)
    local requestGeneration = generation
    local accounts = {}
    local pages = 0
    local seen = {}
    local first = nil
    local function fetch(cursor)
        pages = pages + 1
        local url = base .. route
        if cursor then url = url .. "&cursor=" .. noctalia.string.urlEncode(cursor)
        elseif force then url = url .. "&refresh=true" end
        local accepted = noctalia.http({url = url, headers = {"Accept: application/json", "Authorization: Bearer " .. token}, follow_redirects = false}, function(res)
            if requestGeneration ~= generation then return end
            if not res.ok or res.status ~= 200 then
                local message = "Proxy unreachable. Showing the last successful snapshot."
                if res.status == 401 or res.status == 403 then message = "Management key rejected. Check the key file."
                elseif res.status == 404 then message = "Quota API Extension is unavailable on this proxy."
                elseif res.status and res.status > 0 then message = "Proxy returned HTTP " .. tostring(res.status) .. ". Showing the last snapshot." end
                publishError(message)
                return
            end
            local data = noctalia.json.decode(res.body)
            if type(data) ~= "table" or type(data.summary) ~= "table" or type(data.accounts) ~= "table" then
                publishError("Unexpected quota response. Showing the last snapshot.")
                return
            end
            if not first then first = data end
            for _, account in ipairs(data.accounts) do table.insert(accounts, account) end
            local nextCursor = data.page and data.page.next_cursor
            if nextCursor and nextCursor ~= "" then
                if seen[nextCursor] or pages >= 20 then publishError("Quota pagination did not finish."); return end
                seen[nextCursor] = true
                fetch(nextCursor)
                return
            end
            enrichTiers(accounts, base, token, force, requestGeneration)
            first.accounts = accounts
            first.checked_at = os.time()
            noctalia.state.set("snapshot", first)
            noctalia.state.set("error", "")
            noctalia.state.set("loading", false)
            busy = false
        end)
        if not accepted then publishError("Could not start the quota request.") end
    end
    fetch(nil)
end

noctalia.setUpdateInterval(1000)
noctalia.state.watch("refresh", function(_) refresh(true) end)
function update()
    local interval = math.clamp(tonumber(noctalia.getConfig("poll_seconds")) or 60, 30, 1800)
    if os.time() - lastAttempt >= interval then refresh(false) end
end
function onIpc(event, _)
    if event == "refresh" then refresh(true) end
    if event == "status" then
        local s = noctalia.state.get("snapshot")
        noctalia.log("CPA status: " .. noctalia.json.encode({summary = s and s.summary, error = noctalia.state.get("error"), loading = busy}))
    end
end
function onConfigChanged()
    generation = generation + 1
    tierCache = {}
    noctalia.state.set("tiers", {})
    busy = false
    lastAttempt = 0
    noctalia.state.set("snapshot", nil)
    refresh(false)
end
refresh(false)
