-- dump.lua — evaluate WoW data files (which are plain Lua tables) in a sandbox
-- and print them as JSON on stdout. Run by build.py under lua5.1, the same
-- Lua the game client embeds, so the files are read exactly as the client would.
--
--   lua5.1 dump.lua doc <LuaEnum.lua|-> <file>...
--                                   Blizzard_APIDocumentationGenerated files:
--                                   collects every AddDocumentationTable() call
--   lua5.1 dump.lua res <file>      BlizzardInterfaceResources file: top-level
--                                   `local X = {...}` become keys of the output

local PLACEHOLDER -- set by sandbox(); encodes as null

local function encode(v, out)
    local t = type(v)
    if t == "table" and v == PLACEHOLDER then
        out[#out + 1] = "null"
    elseif t == "table" then
        local n, max = 0, 0
        for k in pairs(v) do
            n = n + 1
            if type(k) == "number" and k > 0 and k == math.floor(k) then
                if k > max then max = k end
            else
                max = -1
                break
            end
        end
        if max >= 0 and max == n then
            out[#out + 1] = "["
            for i = 1, n do
                if i > 1 then out[#out + 1] = "," end
                encode(v[i], out)
            end
            out[#out + 1] = "]"
        else
            out[#out + 1] = "{"
            local first = true
            local keys = {}
            for k in pairs(v) do keys[#keys + 1] = k end
            table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
            for _, k in ipairs(keys) do
                if not first then out[#out + 1] = "," end
                first = false
                encode(tostring(k), out)
                out[#out + 1] = ":"
                encode(v[k], out)
            end
            out[#out + 1] = "}"
        end
    elseif t == "string" then
        out[#out + 1] = '"' .. v:gsub('[%c"\\]', function(c)
            local map = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }
            return map[c] or string.format("\\u%04x", c:byte())
        end) .. '"'
    elseif t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then
            out[#out + 1] = "null"
        elseif v == math.floor(v) and math.abs(v) < 2 ^ 53 then
            out[#out + 1] = string.format("%d", v)
        else
            out[#out + 1] = string.format("%.17g", v)
        end
    elseif t == "boolean" then
        out[#out + 1] = tostring(v)
    else
        out[#out + 1] = "null"
    end
end

local function readfile(path)
    local f = assert(io.open(path, "rb"))
    local s = f:read("*a")
    f:close()
    return s
end

-- An environment where any unknown global resolves to a harmless placeholder,
-- so a file that mentions e.g. Constants.Foo in a Default still loads. Any
-- arithmetic on it yields the placeholder too, which is encoded as null.
local function ph() return PLACEHOLDER end
PLACEHOLDER = setmetatable({}, {
    __index = ph, __add = ph, __sub = ph, __mul = ph, __div = ph, __unm = ph, __concat = ph,
})
local function sandbox()
    local env = {}
    setmetatable(env, { __index = function() return PLACEHOLDER end })
    return env
end

local mode = arg[1]
local result = {}

if mode == "doc" then
    -- arg[2] is the flavor's LuaEnum.lua (or "-"): a few doc files compute
    -- Values from Constants.* / Enum.*, so those must be real tables first.
    local preload = {}
    if arg[2] ~= "-" then
        local chunk = assert(loadstring(readfile(arg[2]), "@" .. arg[2]))
        setfenv(chunk, preload)
        pcall(chunk)
    end
    local tables = {}
    for i = 3, #arg do
        local env = sandbox()
        for k, v in pairs(preload) do
            if type(v) == "table" then
                setmetatable(v, { __index = function() return PLACEHOLDER end })
            end
            rawset(env, k, v)
        end
        env.APIDocumentation = {
            AddDocumentationTable = function(_, t)
                t._file = arg[i]:match("[^/]+$")
                tables[#tables + 1] = t
            end,
        }
        local chunk, err = loadstring(readfile(arg[i]), "@" .. arg[i])
        if not chunk then
            io.stderr:write("parse error: " .. err .. "\n")
        else
            setfenv(chunk, env)
            local ok, rerr = pcall(chunk)
            if not ok then io.stderr:write("run error: " .. tostring(rerr) .. "\n") end
        end
    end
    result = tables
elseif mode == "res" then
    local src = readfile(arg[2])
    -- promote file-scope locals to globals of the sandbox so we can collect them
    src = ("\n" .. src):gsub("\nlocal ([%w_]+)%s*=", "\n%1 =")
    local env = sandbox()
    local chunk = assert(loadstring(src, "@" .. arg[2]))
    setfenv(chunk, env)
    local ok, err = pcall(chunk)
    if not ok then io.stderr:write("run error: " .. tostring(err) .. "\n") end
    setmetatable(env, nil)
    result = env
else
    io.stderr:write("usage: lua5.1 dump.lua doc|res <file>...\n")
    os.exit(2)
end

local out = {}
encode(result, out)
io.write(table.concat(out))
