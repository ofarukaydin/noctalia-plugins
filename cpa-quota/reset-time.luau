local M = {}
-- Convert a civil UTC date to Unix seconds without using local-time os.time().
function M.parse(value)
    if type(value) == "number" then return value > 100000000000 and value / 1000 or value end
    if type(value) ~= "string" then return nil end
    local y,m,d,h,mi,se,tail = value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)T(%d%d):(%d%d):(%d%d)(.*)$")
    if not y then return nil end
    y,m,d,h,mi,se = tonumber(y),tonumber(m),tonumber(d),tonumber(h),tonumber(mi),tonumber(se)
    if m < 1 or m > 12 or d < 1 or d > 31 or h > 23 or mi > 59 or se > 60 then return nil end
    tail = tail:gsub("^%.%d+", "")
    local offset = 0
    if tail ~= "Z" then
        local sign,oh,om = tail:match("^([+-])(%d%d):(%d%d)$")
        if not sign or tonumber(oh) > 23 or tonumber(om) > 59 then return nil end
        offset = (tonumber(oh) * 3600 + tonumber(om) * 60) * (sign == "+" and 1 or -1)
    end
    y = y - (m <= 2 and 1 or 0)
    local era = math.floor(y / 400)
    local yo = y - era * 400
    local doy = math.floor((153 * (m + (m > 2 and -3 or 9)) + 2) / 5) + d - 1
    local doe = yo * 365 + math.floor(yo / 4) - math.floor(yo / 100) + doy
    return (era * 146097 + doe - 719468) * 86400 + h * 3600 + mi * 60 + se - offset
end
function M.relative(epoch, now)
    local seconds = epoch - now
    if seconds <= 0 then return "reset due" end
    local minutes = math.ceil(seconds / 60)
    local days = math.floor(minutes / 1440)
    local hours = math.floor(minutes / 60) % 24
    if days > 0 then return string.format("in %dd %dh", days, hours) end
    if minutes >= 60 then return string.format("in %dh %dm", math.floor(minutes / 60), minutes % 60) end
    return string.format("in %dm", minutes)
end
return M
