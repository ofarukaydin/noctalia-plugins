-- Owns all bulb I/O. Other entries publish a "request" state value; this
-- service queues it, runs aidot.py, and publishes "devices" / "error".
local busy = false
local queue = {}
local jobs = {}
local lastPoll = 0
local setGeneration = 0
local devices = noctalia.state.get("devices") or {}

local function argv(args)
    local cmd = {"python3", "-I", noctalia.pluginDir() .. "/aidot.py",
        "--credentials", noctalia.expandPath(noctalia.getConfig("credentials_file") or "")}
    for _, value in ipairs(args) do table.insert(cmd, value) end
    return cmd
end

local function failure(result)
    local decoded = result and noctalia.json.decode(result.stderr or "")
    if type(decoded) == "table" and decoded.error then return decoded.error end
    if result and result.timedOut then return "aidot.py timed out" end
    local stderr = result and noctalia.string.trim(result.stderr or "") or ""
    return stderr ~= "" and stderr:sub(-200) or "aidot.py failed to run"
end

local function publish()
    noctalia.state.set("devices", devices)
end

-- Which reported fields a change of each kind owns (see apply()).
local FIELD_KEYS = {power = {"on"}, brightness = {"brightness"}, cct = {"cct", "mode"}, color = {"rgb", "mode"}}

-- `onlyFields`: for a change's result, copy just the fields that change set.
-- The rest of its report can be older than what other pending changes show.
local function merge(list, replace, onlyFields)
    if replace then
        local previous = {}
        for _, d in ipairs(devices) do previous[d.id] = d end
        for i, d in ipairs(list) do
            local before = previous[d.id]
            if before and not d.online then
                -- an offline poll keeps the last known values for display
                local stale = table.clone(before)
                stale.online, stale.error = false, d.error
                list[i] = stale
            elseif before then
                -- keep values a partial report omitted (e.g. the CCT while in colour mode)
                for k, v in pairs(before) do
                    if d[k] == nil and k ~= "error" then d[k] = v end
                end
            end
        end
        devices = list
    else
        local keys = nil
        if onlyFields then
            keys = {online = true, error = true}
            for _, field in ipairs(onlyFields) do
                for _, k in ipairs(FIELD_KEYS[field] or {}) do keys[k] = true end
            end
        end
        for _, update in ipairs(list) do
            for _, d in ipairs(devices) do
                if d.id == update.id then
                    for k, v in pairs(update) do
                        if keys == nil or keys[k] then d[k] = v end
                    end
                    if update.error == nil then d.error = nil end
                end
            end
        end
    end
    publish()
end

local function pump()
    if busy then return end
    local key = table.remove(queue, 1)
    if key == nil then return end
    local job = jobs[key]
    jobs[key] = nil
    busy = true
    noctalia.state.set("busy", true)
    local generation = setGeneration
    local launched = noctalia.runAsync(argv(job.args), function(result)
        busy = false
        noctalia.state.set("busy", #queue > 0)
        if job.after then
            job.after(result)
        elseif type(result) == "table" and result.exitCode == 0 then
            local data = noctalia.json.decode(result.stdout or "")
            if type(data) == "table" and type(data.devices) == "table" then
                noctalia.state.set("error", "")
                if job.poll then
                    -- a poll that raced a change would snap controls back to the old value
                    if generation == setGeneration then merge(data.devices, true) end
                elseif jobs[key] == nil then
                    -- while a slider is dragged a newer value is already queued; showing this
                    -- older result would make the control jump back and forth
                    merge(data.devices, false, job.fields)
                end
            else
                noctalia.state.set("error", "Unexpected output from aidot.py")
            end
        else
            noctalia.state.set("error", failure(result))
        end
        pump()
    end)
    if not launched then
        busy = false
        noctalia.state.set("busy", false)
        noctalia.state.set("error", "Could not start python3")
    end
end

-- Jobs with the same key coalesce: a dragged slider only sends its latest value.
local function enqueue(key, job)
    if jobs[key] == nil then table.insert(queue, key) end
    jobs[key] = job
    pump()
end

local function poll()
    lastPoll = os.time()
    enqueue("status", {poll = true, args = {"status"}})
end

-- Music sync runs as a long-lived `aidot.py music` stream. It restores the
-- lights itself when stopped; polling pauses while it owns the bulbs.
local music = {active = false}
local lastMusicLine = 0
local musicGeneration = 0

-- Style and pulse are panel choices, remembered across restarts.
local prefsPath = (noctalia.pluginDataDir() or noctalia.expandPath("~/.cache/aidot-lights")) .. "/prefs.json"
local prefs = noctalia.json.decode(noctalia.readFile(prefsPath) or "") or {}
if type(prefs) ~= "table" then prefs = {} end

local function savePrefs()
    noctalia.writeFile(prefsPath, noctalia.json.encode(prefs))
end

local function publishMusic()
    music.style = (prefs.style == "smooth" or prefs.style == "melody" or prefs.style == "auto") and prefs.style or "beat"
    music.pulse = prefs.pulse == true
    noctalia.state.set("music", music)
end

local function quote(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function stopMusic()
    if not music.active and not music.starting then return end
    musicGeneration = musicGeneration + 1
    music = {active = false}
    publishMusic()
    enqueue("music-stop", {args = {"music-stop"}, after = function(_) poll() end})
end

local function startMusic()
    local style = (prefs.style == "smooth" or prefs.style == "melody" or prefs.style == "auto") and prefs.style or "beat"
    local cmd = table.concat({"exec python3 -I", quote(noctalia.pluginDir() .. "/aidot.py"),
        "--credentials", quote(noctalia.expandPath(noctalia.getConfig("credentials_file") or "")),
        "music --style", style, prefs.pulse and "--pulse" or ""}, " ")
    musicGeneration = musicGeneration + 1
    local generation = musicGeneration
    music = {active = false, starting = true}
    lastMusicLine = os.time()
    publishMusic()
    local launched = noctalia.runStream(cmd, function(line)
        -- a replaced stream still reports its own shutdown; ignore it
        if generation ~= musicGeneration then return end
        local event = noctalia.json.decode(line)
        if type(event) ~= "table" then return end
        lastMusicLine = os.time()
        if event.event == "started" then
            music = {active = true, lights = event.lights}
        elseif event.event == "level" and music.active then
            music.rgb, music.level, music.connected = event.rgb, event.level, event.connected
            music.bpm, music.engine = event.bpm, event.engine
            music.section, music.notes, music.mode = event.section, event.notes, event.mode
        elseif event.event == "error" then
            noctalia.state.set("error", "Music sync: " .. tostring(event.message))
        elseif event.event == "stopped" then
            music = {active = false}
        else
            return
        end
        publishMusic()
    end)
    if not launched then
        music = {active = false}
        publishMusic()
        noctalia.state.set("error", "Could not start music sync")
    end
end

local function apply(request)
    if type(request) ~= "table" then return end
    if request.refresh then
        -- a status check would log in to each bulb and kick music sync's sessions
        if not (music.active or music.starting) then poll() end
        return
    end
    if request.style ~= nil or request.pulse ~= nil then
        if request.style ~= nil then prefs.style = request.style end
        if request.pulse ~= nil then prefs.pulse = request.pulse end
        savePrefs()
        publishMusic()
    end
    if request.music ~= nil then
        -- a new process takes over a running one (and its saved pre-sync state),
        -- so changing style or pulse mid-sync still restores correctly later
        if request.music then startMusic() else stopMusic() end
        return
    end
    if request.ids == nil then return end
    local ids = request.ids
    if type(ids) ~= "table" or #ids == 0 then return end

    local args, fields, optimistic = {"set"}, {}, {}
    for _, id in ipairs(ids) do table.insert(args, id) end
    if request.on ~= nil then
        table.insert(args, request.on and "--on" or "--off"); table.insert(fields, "power")
        optimistic.on = request.on
    end
    if request.brightness then
        table.insert(args, "--brightness"); table.insert(args, tostring(math.floor(request.brightness)))
        table.insert(fields, "brightness"); optimistic.brightness = request.brightness
    end
    if request.cct then
        table.insert(args, "--cct"); table.insert(args, tostring(math.floor(request.cct)))
        table.insert(fields, "cct"); optimistic.cct = request.cct; optimistic.mode = "white"
    end
    if request.rgb then
        table.insert(args, "--rgb"); table.insert(args, request.rgb)
        table.insert(fields, "color"); optimistic.rgb = request.rgb; optimistic.mode = "color"
    end
    if #fields == 0 then return end
    if request.on == nil then optimistic.on = true end
    stopMusic()

    setGeneration = setGeneration + 1
    local wanted = {}
    for _, id in ipairs(ids) do wanted[id] = true end
    for _, d in ipairs(devices) do
        if wanted.all or wanted[d.id] then
            for k, v in pairs(optimistic) do d[k] = v end
        end
    end
    publish()
    table.sort(ids)
    -- Matter's cached state lags a change by up to a second; don't poll right after one
    lastPoll = os.time()
    enqueue(table.concat(ids, ",") .. ":" .. table.concat(fields, "+"), {args = args, fields = fields})
end

noctalia.state.watch("request", function(_) apply(noctalia.state.get("request")) end)
noctalia.setUpdateInterval(1000)

function update()
    if music.active or music.starting then
        -- the stream reports 4x a second; silence means the process died
        if os.time() - lastMusicLine > 8 then
            music = {active = false}
            publishMusic()
            noctalia.state.set("error", "Music sync stopped unexpectedly")
            poll()
        end
        return
    end
    local interval = math.clamp(tonumber(noctalia.getConfig("poll_seconds")) or 20, 5, 600)
    if not busy and os.time() - lastPoll >= interval then poll() end
end

function onIpc(event, payload)
    if event == "refresh" then poll()
    elseif event == "music" then
        if payload == "pulse" or payload == "full" then
            apply({music = true, pulse = payload == "pulse"})
        else
            apply({music = payload ~= "off", style = (payload == "beat" or payload == "smooth" or payload == "melody" or payload == "auto") and payload or nil})
        end
    elseif event == "on" or event == "off" then
        apply({ids = {payload ~= nil and payload ~= "" and payload or "all"}, on = event == "on"})
    end
end

function onConfigChanged()
    devices = {}
    publish()
    poll()
end

publishMusic()
-- a reload kills our stream; make sure an orphaned sync is stopped and the lights restored
if noctalia.fileExists(noctalia.expandPath("~/.cache/aidot-lights/music.pid")) then
    enqueue("music-stop", {args = {"music-stop"}, after = function(_) poll() end})
end
poll()
