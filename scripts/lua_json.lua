-- Local JSON codec for the ReaScript mailbox (RFC 8259 value subset).

local M = {}
M.null = setmetatable({}, { __tostring = function() return "null" end })
M.array_mt = { __json_array = true }

function M.array(values)
  return setmetatable(values or {}, M.array_mt)
end

local function utf8_char(codepoint)
  if codepoint <= 0x7F then
    return string.char(codepoint)
  elseif codepoint <= 0x7FF then
    return string.char(0xC0 + math.floor(codepoint / 0x40),
                       0x80 + codepoint % 0x40)
  elseif codepoint <= 0xFFFF then
    return string.char(0xE0 + math.floor(codepoint / 0x1000),
                       0x80 + math.floor(codepoint / 0x40) % 0x40,
                       0x80 + codepoint % 0x40)
  end
  return string.char(0xF0 + math.floor(codepoint / 0x40000),
                     0x80 + math.floor(codepoint / 0x1000) % 0x40,
                     0x80 + math.floor(codepoint / 0x40) % 0x40,
                     0x80 + codepoint % 0x40)
end

local function decode_string(text, position)
  local parts = {}
  local chunk_start = position + 1
  position = position + 1
  while position <= #text do
    local byte = text:byte(position)
    if byte == 34 then
      parts[#parts + 1] = text:sub(chunk_start, position - 1)
      return table.concat(parts), position + 1
    elseif byte == 92 then
      parts[#parts + 1] = text:sub(chunk_start, position - 1)
      local escaped = text:sub(position + 1, position + 1)
      local simple = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
                       b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }
      if simple[escaped] then
        parts[#parts + 1] = simple[escaped]
        position = position + 2
      elseif escaped == 'u' then
        local hex = text:sub(position + 2, position + 5)
        local codepoint = tonumber(hex, 16)
        if not codepoint then error("invalid Unicode escape at byte " .. position) end
        position = position + 6
        if codepoint >= 0xD800 and codepoint <= 0xDBFF and
           text:sub(position, position + 1) == '\\u' then
          local low = tonumber(text:sub(position + 2, position + 5), 16)
          if low and low >= 0xDC00 and low <= 0xDFFF then
            codepoint = 0x10000 + (codepoint - 0xD800) * 0x400 + (low - 0xDC00)
            position = position + 6
          else
            codepoint = 0xFFFD
          end
        elseif codepoint >= 0xDC00 and codepoint <= 0xDFFF then
          codepoint = 0xFFFD
        end
        parts[#parts + 1] = utf8_char(codepoint)
      else
        error("invalid string escape at byte " .. position)
      end
      chunk_start = position
    elseif byte < 32 then
      error("unescaped control character at byte " .. position)
    else
      position = position + 1
    end
  end
  error("unterminated JSON string")
end

local function parse_number(text, position)
  local tail = text:sub(position)
  local token = tail:match("^-?%d+%.%d+[eE][+-]?%d+") or
                tail:match("^-?%d+[eE][+-]?%d+") or
                tail:match("^-?%d+%.%d+") or
                tail:match("^-?%d+")
  if not token then
    error("invalid JSON number at byte " .. position)
  end
  -- RFC 8259 forbids leading zeros, but 0.5 / 0e3 are valid. The previous
  -- check rejected every token starting with 0, which made "0.0" from Python
  -- an invalid request and silently killed tempo and transaction calls.
  local sign_offset = token:sub(1, 1) == '-' and 2 or 1
  local first_digit = token:sub(sign_offset, sign_offset)
  local next_char = token:sub(sign_offset + 1, sign_offset + 1)
  if first_digit == '0' and next_char ~= '' and next_char ~= '.' and
     next_char ~= 'e' and next_char ~= 'E' then
    error("invalid JSON number at byte " .. position)
  end
  local value = tonumber(token)
  if not value or value ~= value or value == math.huge or value == -math.huge then
    error("JSON number is outside the supported range")
  end
  return value, position + #token
end

function M.decode(text)
  if type(text) ~= "string" then error("JSON input must be a string") end
  local position = 1
  local function whitespace()
    while position <= #text and text:sub(position, position):match("[%s]") do
      position = position + 1
    end
  end
  local parse_value
  parse_value = function(depth)
    if depth > 128 then error("JSON nesting limit exceeded") end
    whitespace()
    local first = text:sub(position, position)
    if first == '"' then
      local value
      value, position = decode_string(text, position)
      return value
    elseif first == '{' then
      position = position + 1
      local object = {}
      whitespace()
      if text:sub(position, position) == '}' then position = position + 1; return object end
      while true do
        whitespace()
        if text:sub(position, position) ~= '"' then error("object key must be a string") end
        local key
        key, position = decode_string(text, position)
        whitespace()
        if text:sub(position, position) ~= ':' then error("expected ':' in object") end
        position = position + 1
        object[key] = parse_value(depth + 1)
        whitespace()
        local separator = text:sub(position, position)
        if separator == '}' then position = position + 1; return object end
        if separator ~= ',' then error("expected ',' or '}' in object") end
        position = position + 1
      end
    elseif first == '[' then
      position = position + 1
      local array = M.array()
      whitespace()
      if text:sub(position, position) == ']' then position = position + 1; return array end
      while true do
        array[#array + 1] = parse_value(depth + 1)
        whitespace()
        local separator = text:sub(position, position)
        if separator == ']' then position = position + 1; return array end
        if separator ~= ',' then error("expected ',' or ']' in array") end
        position = position + 1
      end
    elseif text:sub(position, position + 3) == 'true' then
      position = position + 4
      return true
    elseif text:sub(position, position + 4) == 'false' then
      position = position + 5
      return false
    elseif text:sub(position, position + 3) == 'null' then
      position = position + 4
      return M.null
    elseif first == '-' or first:match("%d") then
      local value
      value, position = parse_number(text, position)
      return value
    end
    error("unexpected JSON token at byte " .. position)
  end
  local value = parse_value(0)
  whitespace()
  if position <= #text then error("trailing data after JSON value") end
  return value
end

local function quote(value)
  return '"' .. value:gsub('[%z\1-\31\\"]', function(char)
    local escapes = { ['"'] = '\\"', ['\\'] = '\\\\',
                      ['\b'] = '\\b', ['\f'] = '\\f',
                      ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
    return escapes[char] or string.format('\\u%04x', char:byte())
  end) .. '"'
end

local function is_array(value)
  if getmetatable(value) == M.array_mt then return true end
  local count, highest = 0, 0
  for key in pairs(value) do
    if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then return false end
    count = count + 1
    if key > highest then highest = key end
  end
  return count > 0 and count == highest
end

function M.encode(value)
  local seen = {}
  local function encode(item, depth)
    if depth > 128 then error("JSON nesting limit exceeded") end
    if item == M.null or item == nil then return "null" end
    local kind = type(item)
    if kind == "boolean" then return item and "true" or "false" end
    if kind == "number" then
      if item ~= item or item == math.huge or item == -math.huge then
        error("cannot encode a non-finite number")
      end
      return string.format("%.17g", item):gsub(",", ".")
    end
    if kind == "string" then return quote(item) end
    if kind ~= "table" then error("unsupported JSON value type: " .. kind) end
    if seen[item] then error("cannot encode a cyclic table") end
    seen[item] = true
    local result
    if is_array(item) then
      local parts = {}
      for index = 1, #item do parts[index] = encode(item[index], depth + 1) end
      result = '[' .. table.concat(parts, ',') .. ']'
    else
      local keys = {}
      for key in pairs(item) do
        if type(key) ~= "string" then error("JSON object keys must be strings") end
        keys[#keys + 1] = key
      end
      table.sort(keys)
      local parts = {}
      for _, key in ipairs(keys) do
        parts[#parts + 1] = quote(key) .. ':' .. encode(item[key], depth + 1)
      end
      result = '{' .. table.concat(parts, ',') .. '}'
    end
    seen[item] = nil
    return result
  end
  return encode(value, 0)
end

return M
