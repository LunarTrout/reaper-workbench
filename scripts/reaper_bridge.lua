-- Resident REAPER service for the Python library. Run this ReaScript in the
-- REAPER instance that should be controlled. It uses only ReaScript APIs and
-- local request/response files; it never inspects process memory.
-- Resolve this file's own path. get_action_context() points at the launcher
-- when the bridge is started from __startup.lua, which would break the
-- relative lua_json.lua lookup and leave the bridge unable to answer.
local source = debug.getinfo(1, "S").source or ""
local script_path = source:match("^@(.*)$") or ""
if script_path == "" then
  reaper.ShowMessageBox("Cannot determine the ReaScript file path.",
                        "REAPER Workbench", 0)
  return
end

local script_dir = script_path:match("^(.*)[/\\]") or "."
local json_ok, json = pcall(dofile, script_dir .. "/lua_json.lua")
if not json_ok or type(json) ~= "table" then
  reaper.ShowMessageBox("Cannot load lua_json.lua next to the bridge: " ..
                        tostring(json), "REAPER Workbench", 0)
  return
end
local local_app_data = os.getenv("LOCALAPPDATA")
if not local_app_data or local_app_data == "" then
  reaper.ShowMessageBox("LOCALAPPDATA is not available in this REAPER process.",
                        "REAPER Workbench", 0)
  return
end

local bridge_session_id = string.format("%x-%x", os.time(),
                                        math.floor(reaper.time_precise() * 1000000))
local runtime_root = local_app_data .. "/REAPER-Audio-Library/runtime"
local registry_dir = runtime_root .. "/instances"
local runtime_dir = runtime_root .. "/sessions/" .. bridge_session_id
local response_dir = runtime_dir .. "/responses"
local registry_path = registry_dir .. "/" .. bridge_session_id .. ".json"
local pulse_path = runtime_dir .. "/heartbeat"
local request_path = runtime_dir .. "/request.json"
local protocol_version = 1
local max_request_bytes = 16 * 1024 * 1024
local api_handles = {}
local api_handle_order = {}
local max_api_handles = 4096
local transaction_plans = {}
local transaction_plan_order = {}
local last_transaction_by_project = {}
local max_transaction_plans = 128
local transaction_plan_ttl_s = 900

reaper.RecursiveCreateDirectory(runtime_root .. "/sessions", 0)
reaper.RecursiveCreateDirectory(registry_dir, 0)
reaper.RecursiveCreateDirectory(response_dir, 0)
reaper.set_action_options(1 | 4)

local function finite_number(value)
  return type(value) == "number" and value == value and
         value ~= math.huge and value ~= -math.huge
end

local function fail(code, message, details)
  error({ bridge_error = true, code = code, message = message,
          details = details or {} }, 0)
end

local function active_project()
  local project, path = reaper.EnumProjects(-1, "")
  if not project then error("No active REAPER project") end
  return project, path or ""
end

local function project_identity(project, path)
  local master = reaper.GetMasterTrack(project)
  local master_guid = master and reaper.GetTrackGUID(master) or ""
  return { project_id = "reaper:" .. master_guid .. ":" .. path,
           identity_basis = "master_track_guid_and_project_path",
           path = path,
           change_count = reaper.GetProjectStateChangeCount(project) }
end

local function resolve_track(project, guid)
  if type(guid) ~= "string" or guid == "" then return nil end
  local master = reaper.GetMasterTrack(project)
  if master and reaper.GetTrackGUID(master) == guid then return master, -1 end
  local count = reaper.CountTracks(project)
  for index = 0, count - 1 do
    local track = reaper.GetTrack(project, index)
    if track and reaper.GetTrackGUID(track) == guid then return track, index end
  end
  return nil
end

local function project_path_for(project)
  local index = 0
  while true do
    local candidate, path = reaper.EnumProjects(index, "")
    if not candidate then return nil end
    if candidate == project then return path or "" end
    index = index + 1
  end
end

local function find_project(project_id)
  local index = 0
  while true do
    local project, path = reaper.EnumProjects(index, "")
    if not project then return nil end
    if project_identity(project, path or "").project_id == project_id then
      return project, path or ""
    end
    index = index + 1
  end
end

local function target_project(arguments)
  local project_id = arguments and arguments.project_id
  if project_id == nil or project_id == json.null or project_id == "" then
    return active_project()
  end
  if type(project_id) ~= "string" then
    fail("validation_error", "project_id must be a string")
  end
  local project, path = find_project(project_id)
  if not project then
    fail("stale_context", "The requested project is not open in this REAPER instance",
         { project_id = project_id })
  end
  return project, path
end

local function resolve_item(project, guid)
  if type(guid) ~= "string" or guid == "" then return nil end
  local function inspect_track(track)
    for index = 0, reaper.CountTrackMediaItems(track) - 1 do
      local item = reaper.GetTrackMediaItem(track, index)
      local ok, item_guid = reaper.GetSetMediaItemInfo_String(item, "GUID", "", false)
      if ok and item_guid == guid then return item end
    end
    return nil
  end
  local master = reaper.GetMasterTrack(project)
  local item = master and inspect_track(master) or nil
  if item then return item end
  for index = 0, reaper.CountTracks(project) - 1 do
    item = inspect_track(reaper.GetTrack(project, index))
    if item then return item end
  end
  return nil
end

local function resolve_take(project, guid)
  if type(guid) ~= "string" or guid == "" then return nil end
  local function inspect_track(track)
    for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
      local item = reaper.GetTrackMediaItem(track, item_index)
      for take_index = 0, reaper.CountTakes(item) - 1 do
        local take = reaper.GetMediaItemTake(item, take_index)
        local ok, take_guid = reaper.GetSetMediaItemTakeInfo_String(take, "GUID", "", false)
        if ok and take_guid == guid then return take end
      end
    end
    return nil
  end
  local master = reaper.GetMasterTrack(project)
  local take = master and inspect_track(master) or nil
  if take then return take end
  for index = 0, reaper.CountTracks(project) - 1 do
    take = inspect_track(reaper.GetTrack(project, index))
    if take then return take end
  end
  return nil
end

local api_handle_types = {
  AudioAccessor = "AudioAccessor*",
  HWND = "HWND",
  IReaperControlSurface = "IReaperControlSurface*",
  KbdSectionInfo = "KbdSectionInfo*",
  MediaItem = "MediaItem*",
  MediaItem_Take = "MediaItem_Take*",
  MediaTrack = "MediaTrack*",
  PCM_source = "PCM_source*",
  ProjectMarker = "ProjectMarker*",
  ReaProject = "ReaProject*",
  TrackEnvelope = "TrackEnvelope*",
  joystick_device = "joystick_device*",
}

local function stable_handle_id(value, kind, project, path)
  if kind == "ReaProject" then
    return project_identity(value, project_path_for(value) or path or "").project_id
  elseif kind == "MediaTrack" then
    return reaper.GetTrackGUID(value)
  elseif kind == "MediaItem" then
    local ok, guid = reaper.GetSetMediaItemInfo_String(value, "GUID", "", false)
    return ok and guid or nil
  elseif kind == "MediaItem_Take" then
    local ok, guid = reaper.GetSetMediaItemTakeInfo_String(value, "GUID", "", false)
    return ok and guid or nil
  end
  return nil
end

local function remember_api_handle(value, kind, project, path, change_count, request_id, return_index)
  local handle_project = kind == "ReaProject" and value or project
  local handle_path = project_path_for(handle_project)
  if not handle_path then
    fail("stale_context", "API returned a host object from a project that is no longer open",
         { kind = kind })
  end
  local revision = project_identity(handle_project, handle_path)
  local stable_id = stable_handle_id(value, kind, handle_project, handle_path)
  local descriptor = { kind = kind, project_id = revision.project_id,
                       change_count = revision.change_count }
  if stable_id then
    descriptor.stable_id = stable_id
  else
    local token = request_id .. ":" .. tostring(return_index)
    descriptor.token = token
    descriptor.bridge_session = bridge_session_id
    api_handles[token] = { value = value, kind = kind,
                           project_id = revision.project_id,
                           change_count = revision.change_count,
                           c_type = api_handle_types[kind],
                           project = handle_project }
    api_handle_order[#api_handle_order + 1] = token
    while #api_handle_order > max_api_handles do
      local expired = table.remove(api_handle_order, 1)
      api_handles[expired] = nil
    end
  end
  return { ["$reaper_handle"] = descriptor }
end

local function resolve_api_handle(encoded, expected_kind)
  local descriptor = type(encoded) == "table" and encoded["$reaper_handle"] or nil
  if type(descriptor) ~= "table" then
    fail("validation_error", "Expected a typed REAPER handle")
  end
  if descriptor.kind ~= expected_kind then
    fail("validation_error", "REAPER handle type does not match API signature",
         { expected = expected_kind, received = descriptor.kind })
  end
  local project_id = descriptor.project_id
  local project, path = find_project(project_id)
  if not project then
    fail("stale_context", "The project associated with this REAPER handle is no longer open",
         { project_id = project_id })
  end
  local change_count = tonumber(descriptor.change_count)
  local current_count = reaper.GetProjectStateChangeCount(project)
  if not change_count or change_count ~= current_count then
    fail("stale_context", "The project changed after this REAPER handle was created",
         { project_id = project_id, expected_change_count = change_count,
           current_change_count = current_count })
  end
  if descriptor.stable_id then
    local value
    if expected_kind == "ReaProject" and descriptor.stable_id == project_id then
      value = project
    elseif expected_kind == "MediaTrack" then
      value = resolve_track(project, descriptor.stable_id)
    elseif expected_kind == "MediaItem" then
      value = resolve_item(project, descriptor.stable_id)
    elseif expected_kind == "MediaItem_Take" then
      value = resolve_take(project, descriptor.stable_id)
    else
      fail("validation_error", "This REAPER handle type has no stable-ID resolver",
           { kind = expected_kind })
    end
    if not value then
      fail("stale_context", "The REAPER object no longer exists",
           { kind = expected_kind, stable_id = descriptor.stable_id })
    end
    return value, project, path
  end
  if descriptor.bridge_session ~= bridge_session_id then
    fail("stale_context", "Opaque REAPER handles expire when the bridge restarts",
         { kind = expected_kind })
  end
  local saved = api_handles[descriptor.token]
  if not saved or saved.kind ~= expected_kind or saved.project_id ~= project_id or
     saved.change_count ~= change_count then
    fail("stale_context", "Opaque REAPER handle is missing or expired",
         { kind = expected_kind })
  end
  if saved.c_type and reaper.APIExists("ValidatePtr2") and
     expected_kind ~= "HWND" and expected_kind ~= "KbdSectionInfo" and
     expected_kind ~= "IReaperControlSurface" and expected_kind ~= "joystick_device" and
     not reaper.ValidatePtr2(project, saved.value, saved.c_type) then
    fail("stale_context", "REAPER no longer recognizes this host object",
         { kind = expected_kind })
  end
  return saved.value, project, path
end

local function get_track_name(track)
  local ok, value = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
  if ok then return value end
  return ""
end

local function fx_context(track, include_params)
  local result = json.array()
  local count = reaper.TrackFX_GetCount(track)
  for fx_index = 0, count - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, fx_index, "")
    local fx = {
      index = fx_index,
      guid = reaper.TrackFX_GetFXGUID(track, fx_index),
      name = name or "",
      enabled = reaper.TrackFX_GetEnabled(track, fx_index),
      offline = reaper.TrackFX_GetOffline(track, fx_index),
      parameter_count = reaper.TrackFX_GetNumParams(track, fx_index),
    }
    if include_params then
      fx.parameters = json.array()
      for param_index = 0, fx.parameter_count - 1 do
        local _, param_name = reaper.TrackFX_GetParamName(track, fx_index, param_index, "")
        local value = reaper.TrackFX_GetParamNormalized(track, fx_index, param_index)
        local _, formatted = reaper.TrackFX_GetFormattedParamValue(
          track, fx_index, param_index, "")
        fx.parameters[#fx.parameters + 1] = {
          index = param_index,
          name = param_name or "",
          normalized = value,
          formatted = formatted or "",
        }
      end
    end
    result[#result + 1] = fx
  end
  return result
end

local function item_context(project, track, item_index, options)
  local item = reaper.GetTrackMediaItem(track, item_index)
  local take = item and reaper.GetActiveTake(item) or nil
  local item_guid_ok, item_guid = false, ""
  if item then item_guid_ok, item_guid = reaper.GetSetMediaItemInfo_String(
    item, "GUID", "", false) end
  local entry = {
    guid = item_guid_ok and item_guid or "",
    position_s = item and reaper.GetMediaItemInfo_Value(item, "D_POSITION") or 0,
    length_s = item and reaper.GetMediaItemInfo_Value(item, "D_LENGTH") or 0,
    fade_in_s = item and reaper.GetMediaItemInfo_Value(item, "D_FADEINLEN") or 0,
    fade_out_s = item and reaper.GetMediaItemInfo_Value(item, "D_FADEOUTLEN") or 0,
    mute = item and reaper.GetMediaItemInfo_Value(item, "B_MUTE") ~= 0 or false,
    selected = item and reaper.IsMediaItemSelected(item) or false,
    take_count = item and reaper.CountTakes(item) or 0,
    active_take = json.null,
  }
  if take then
    local take_guid_ok, take_guid = reaper.GetSetMediaItemTakeInfo_String(
      take, "GUID", "", false)
    local source = reaper.GetMediaItemTake_Source(take)
    local source_file = source and reaper.GetMediaSourceFileName(source, "") or ""
    entry.active_take = {
      guid = take_guid_ok and take_guid or "",
      name = reaper.GetTakeName(take) or "",
      source = source_file or "",
      is_midi = reaper.TakeIsMIDI(take),
      playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE"),
      preserve_pitch = reaper.GetMediaItemTakeInfo_Value(take, "B_PPITCH") ~= 0,
      stretch_markers = reaper.GetTakeNumStretchMarkers(take),
      fx_count = reaper.TakeFX_GetCount(take),
    }
    if options.include_midi_notes == true and entry.active_take.is_midi then
      local ok, note_count, cc_count, text_count = reaper.MIDI_CountEvts(take)
      if ok then
        local limit = 10000
        local notes = json.array()
        local returned = math.min(note_count, limit)
        for note_index = 0, returned - 1 do
          local note_ok, selected, muted, start_ppq, end_ppq, channel, pitch, velocity =
            reaper.MIDI_GetNote(take, note_index)
          if note_ok then
            notes[#notes + 1] = {
              index = note_index,
              selected = selected,
              muted = muted,
              start_ppq = start_ppq,
              end_ppq = end_ppq,
              start_s = reaper.MIDI_GetProjTimeFromPPQPos(take, start_ppq),
              end_s = reaper.MIDI_GetProjTimeFromPPQPos(take, end_ppq),
              channel = channel,
              pitch = pitch,
              velocity = velocity,
            }
          end
        end
        entry.active_take.midi = {
          note_count = note_count,
          cc_count = cc_count,
          text_event_count = text_count,
          notes = notes,
          returned_note_count = #notes,
          truncated = note_count > limit,
        }
      else
        entry.active_take.midi = { error = "MIDI_CountEvts failed" }
      end
    end
  end
  return entry
end

local function track_context(project, track, index, options)
  local entry = {
    guid = reaper.GetTrackGUID(track),
    index = index,
    name = get_track_name(track),
    is_master = index == -1,
    selected = reaper.IsTrackSelected(track),
    volume = reaper.GetMediaTrackInfo_Value(track, "D_VOL"),
    pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN"),
    mute = reaper.GetMediaTrackInfo_Value(track, "B_MUTE") ~= 0,
    solo = reaper.GetMediaTrackInfo_Value(track, "I_SOLO"),
    record_arm = reaper.GetMediaTrackInfo_Value(track, "I_RECARM"),
    record_mode = reaper.GetMediaTrackInfo_Value(track, "I_RECMODE"),
    record_mode_flags = reaper.GetMediaTrackInfo_Value(track, "I_RECMODE_FLAGS"),
    channel_count = reaper.GetMediaTrackInfo_Value(track, "I_NCHAN"),
    folder_depth = reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH"),
    fx = fx_context(track, options.include_fx_params == true),
    sends = reaper.GetTrackNumSends(track, 0),
    receives = reaper.GetTrackNumSends(track, -1),
    hardware_sends = reaper.GetTrackNumSends(track, 1),
  }
  if options.include_items == true then
    entry.items = json.array()
    for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
      entry.items[#entry.items + 1] = item_context(project, track, item_index, options)
    end
  end
  return entry
end

local function project_context(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity(project, path)
  local tracks = json.array()
  local count = reaper.CountTracks(project)
  for index = 0, count - 1 do
    tracks[#tracks + 1] = track_context(project, reaper.GetTrack(project, index),
                                       index, arguments)
  end
  local master = reaper.GetMasterTrack(project)
  local numerator, denominator, bpm = reaper.TimeMap_GetTimeSigAtTime(project, 0)
  local tempo_map = json.array()
  local tempo_count = reaper.CountTempoTimeSigMarkers(project)
  for index = 0, tempo_count - 1 do
    local ok, time_s, measure, beat, marker_bpm, num, den, linear =
      reaper.GetTempoTimeSigMarker(project, index)
    if ok then
      tempo_map[#tempo_map + 1] = { time_s = time_s, measure = measure,
                                    beat = beat, bpm = marker_bpm,
                                    numerator = num, denominator = den,
                                    linear = linear }
    end
  end
  local selected_tracks = json.array()
  for index = 0, reaper.CountSelectedTracks(project) - 1 do
    local track = reaper.GetSelectedTrack(project, index)
    selected_tracks[#selected_tracks + 1] = reaper.GetTrackGUID(track)
  end
  local selection_start, selection_end = reaper.GetSet_LoopTimeRange2(
    project, false, false, 0, 0, false)
  return {
    revision = revision,
    path = path,
    name = reaper.GetProjectName(project),
    track_count = count,
    tracks = tracks,
    length_s = reaper.GetProjectLength(project),
    master = track_context(project, master, -1, arguments),
    selection = { track_guids = selected_tracks,
                 time_start_s = selection_start, time_end_s = selection_end },
    transport = { play_state = reaper.GetPlayStateEx(project),
                  cursor_s = reaper.GetCursorPositionEx(project),
                  play_position_s = reaper.GetPlayPositionEx(project) },
    tempo = { bpm = bpm, numerator = numerator, denominator = denominator,
              markers = tempo_map },
  }
end

local function project_tempo_at(arguments)
  local project, path = target_project(arguments)
  local times = arguments and arguments.times_s
  if type(times) ~= "table" then
    fail("validation_error", "times_s must be an array of project-time seconds")
  end
  if #times > 50000 then
    fail("validation_error", "times_s exceeds the 50000-point batch limit",
         { received = #times, maximum = 50000 })
  end
  local tempo_marker_count = reaper.CountTempoTimeSigMarkers(project)
  local base_numerator, base_denominator, base_bpm
  if tempo_marker_count == 0 then
    -- Match project_context's effective value at the timeline start. REAPER
    -- 7.84 did not answer when this query used a negative time position.
    base_numerator, base_denominator, base_bpm =
      reaper.TimeMap_GetTimeSigAtTime(project, 0.0)
  end
  local points = json.array()
  for index = 1, #times do
    local time_s = times[index]
    if not finite_number(time_s) then
      fail("validation_error", "Every project time must be a finite number",
           { index = index })
    end
    local numerator, denominator, bpm
    if tempo_marker_count == 0 then
      numerator, denominator, bpm = base_numerator, base_denominator, base_bpm
    else
      numerator, denominator, bpm = reaper.TimeMap_GetTimeSigAtTime(project, time_s)
    end
    if not finite_number(bpm) or bpm <= 0 or
       not finite_number(numerator) or numerator <= 0 or
       not finite_number(denominator) or denominator <= 0 then
      -- Never place NaN/Infinity in error details: the JSON encoder cannot
      -- represent them, which would otherwise suppress the response and turn
      -- a useful API error into a client-side timeout.
      fail("api_unavailable", "REAPER returned an invalid effective tempo",
           { time_s = time_s, result = "non_finite_or_non_positive" })
    end
    points[#points + 1] = {
      time_s = time_s,
      bpm = bpm,
      numerator = numerator,
      denominator = denominator,
      effective_quarter_bpm = bpm * 4 / denominator,
    }
  end
  local revision = project_identity(project, path)
  local source = tempo_marker_count == 0 and "project.base_tempo_no_markers" or
                 "TimeMap_GetTimeSigAtTime"
  return { project_id = revision.project_id, revision = revision,
           points = points, source = source }
end

local function project_render_settings_read(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity(project, path)
  local settings = reaper.GetSetProjectInfo(project, "RENDER_SETTINGS", 0, false)
  if not finite_number(settings) then
    fail("api_unavailable", "REAPER returned an invalid RENDER_SETTINGS value")
  end
  local targets_ok, targets = reaper.GetSetProjectInfo_String(
    project, "RENDER_TARGETS", "", false)
  -- An empty target list is valid (e.g. after a fresh project load); the
  -- capture path writes its own targets before rendering. Only a non-string
  -- answer is an API failure.
  if type(targets) ~= "string" then
    fail("api_unavailable", "REAPER returned an invalid render target list")
  end
  local dirty = reaper.GetSetProjectInfo(project, "DIRTY", 0, false)
  if not finite_number(dirty) then
    fail("api_unavailable", "REAPER returned an invalid project dirty state")
  end
  return { project_id = revision.project_id, revision = revision,
           render_settings = settings, render_targets = targets, dirty = dirty }
end

local function project_capture_render(arguments)
  local project, path = target_project(arguments)
  local expected = arguments.expected_revision
  local master_only = arguments.master_only == true
  local revision = project_identity(project, path)
  if type(expected) ~= "table" or expected.project_id ~= revision.project_id or
     expected.path ~= revision.path or expected.change_count ~= revision.change_count then
    fail("stale_context", "Project changed before the analysis render",
         { expected = expected, current = revision })
  end
  if reaper.GetPlayStateEx(project) ~= 0 then
    fail("validation_error", "Stop playback before an offline analysis render",
         { play_state = reaper.GetPlayStateEx(project) })
  end
  local output_dir = arguments.output_dir
  local job_id = arguments.job_id
  if type(output_dir) ~= "string" or output_dir == "" or
     not (output_dir:match("^[A-Za-z]:[/\\]") or output_dir:match("^[/\\][/\\]")) then
    fail("validation_error", "output_dir must be an absolute Windows path")
  end
  if type(job_id) ~= "string" or not job_id:match("^[A-Za-z0-9_-]+$") or #job_id > 64 then
    fail("validation_error", "job_id must contain 1 to 64 safe filename characters")
  end
  local function same_path(left, right)
    local function normalized(value)
      return value:gsub("/", "\\"):gsub("\\+", "\\"):lower()
    end
    return type(left) == "string" and type(right) == "string" and
           normalized(left) == normalized(right)
  end

  local numeric_fields = { "RENDER_SETTINGS", "RENDER_BOUNDSFLAG",
                           "RENDER_CHANNELS", "RENDER_SRATE", "RENDER_ADDTOPROJ",
                           "RENDER_DITHER", "RENDER_NORMALIZE" }
  local string_fields = { "RENDER_FILE", "RENDER_PATTERN", "RENDER_FORMAT",
                          "RENDER_FORMAT2" }
  local prior_numeric, prior_strings = {}, {}
  for _, name in ipairs(numeric_fields) do
    local value = reaper.GetSetProjectInfo(project, name, 0, false)
    if not finite_number(value) then
      fail("api_unavailable", "REAPER returned an invalid render setting",
           { setting = name })
    end
    prior_numeric[name] = value
  end
  for _, name in ipairs(string_fields) do
    local ok, value = reaper.GetSetProjectInfo_String(project, name, "", false)
    if not ok or type(value) ~= "string" then
      fail("api_unavailable", "REAPER could not read a render setting",
           { setting = name })
    end
    prior_strings[name] = value
  end
  local prior_dirty = reaper.GetSetProjectInfo(project, "DIRTY", 0, false)
  if not finite_number(prior_dirty) then
    fail("api_unavailable", "REAPER returned an invalid project dirty state")
  end
  local prior_time_start, prior_time_end = reaper.GetSet_LoopTimeRange2(
    project, false, false, 0, 0, false)
  local section_start = arguments.section_start_s
  local section_end = arguments.section_end_s
  local has_section = finite_number(section_start) and finite_number(section_end) and
                      section_end > section_start

  local track_count = reaper.CountTracks(project)
  local tracks, selected = {}, {}
  local capture_targets = {}
  local function restore()
    local failures = {}
    for _, name in ipairs(numeric_fields) do
      local ok, value = pcall(reaper.GetSetProjectInfo, project, name,
                              prior_numeric[name], true)
      if not ok or not finite_number(value) then failures[#failures + 1] = name end
    end
    for _, name in ipairs(string_fields) do
      local ok, value = pcall(reaper.GetSetProjectInfo_String, project, name,
                              prior_strings[name], true)
      if not ok or value == false then failures[#failures + 1] = name end
    end
    for index = 0, #tracks - 1 do
      local ok = pcall(reaper.SetTrackSelected, tracks[index + 1], selected[index + 1])
      if not ok then failures[#failures + 1] = "track_selection:" .. tostring(index) end
    end
    local dirty_ok, dirty_value = pcall(reaper.GetSetProjectInfo, project,
                                        "DIRTY", prior_dirty, true)
    local actual_dirty = reaper.GetSetProjectInfo(project, "DIRTY", 0, false)
    if not dirty_ok or not finite_number(dirty_value) or
       not finite_number(actual_dirty) or math.abs(actual_dirty - prior_dirty) > 1e-9 then
      failures[#failures + 1] = "project_dirty_state"
    end
    local time_ok = pcall(reaper.GetSet_LoopTimeRange2, project, true, false,
                          prior_time_start, prior_time_end, false)
    if not time_ok then failures[#failures + 1] = "time_selection" end
    return failures
  end

  local ok, capture = xpcall(function()
    for index = 0, track_count - 1 do
      local track = reaper.GetTrack(project, index)
      tracks[index + 1] = track
      selected[index + 1] = reaper.IsTrackSelected(track)
      reaper.SetTrackSelected(track, true)
    end

    local function set_number(name, value)
      local ok_set = pcall(reaper.GetSetProjectInfo, project, name, value, true)
      if not ok_set then error("REAPER rejected render setting " .. name) end
      local actual = reaper.GetSetProjectInfo(project, name, 0, false)
      if not finite_number(actual) or math.abs(actual - value) > 1e-9 then
        error("REAPER did not retain render setting " .. name)
      end
    end
    local function set_string(name, value)
      local ok_set, result = reaper.GetSetProjectInfo_String(project, name, value, true)
      if not ok_set or result == false then
        error("REAPER rejected render setting " .. name)
      end
    end

    local prefix = "rali-" .. job_id .. "-"
    set_string("RENDER_FILE", output_dir)
    set_string("RENDER_PATTERN", prefix .. "$tracknumber-$track")
    set_string("RENDER_FORMAT", "evaw")
    set_string("RENDER_FORMAT2", "")
    set_number("RENDER_SETTINGS", master_only and 0 or 5) -- master mix or stems + master
    if has_section then
      local range_ok = reaper.GetSet_LoopTimeRange2(
        project, true, false, section_start, section_end, false)
      if not range_ok then error("REAPER rejected the analysis time selection") end
      set_number("RENDER_BOUNDSFLAG", 2) -- time selection
    else
      set_number("RENDER_BOUNDSFLAG", 1) -- entire project
    end
    set_number("RENDER_CHANNELS", 2)
    set_number("RENDER_SRATE", 0) -- project sample rate
    set_number("RENDER_ADDTOPROJ", 0)
    set_number("RENDER_DITHER", 16) -- disable render dither/noise shaping
    set_number("RENDER_NORMALIZE", 0) -- no normalize/limit/fade postprocessing

    local targets_ok, targets_text = reaper.GetSetProjectInfo_String(
      project, "RENDER_TARGETS", "", false)
    if not targets_ok or type(targets_text) ~= "string" then
      error("REAPER could not build the analysis render target list")
    end
    for target in targets_text:gmatch("[^;]+") do
      local parent = target:match("^(.*)[/\\][^/\\]+$")
      if not same_path(parent, output_dir) or not target:lower():match("%.wav$") then
        error("REAPER produced an unsafe analysis render target: " .. target)
      end
      local existing = io.open(target, "rb")
      if existing then
        existing:close()
        error("Analysis render target already exists: " .. target)
      end
      capture_targets[#capture_targets + 1] = target
    end
    if #capture_targets ~= (master_only and 1 or track_count + 1) then
      error("REAPER did not produce the expected analysis render targets")
    end

    local action_name = reaper.kbd_getTextFromCmd(
      42230, reaper.SectionFromUniqueID(0))
    if type(action_name) ~= "string" or not action_name:lower():find("render project", 1, true) then
      error("The installed REAPER render action 42230 is unavailable")
    end
    reaper.Main_OnCommandEx(42230, 0, project)

    local files, seen_tracks, seen_track_count, master_count = json.array(), {}, 0, 0
    for _, target in ipairs(capture_targets) do
      local name = target:match("([^/\\]+)$") or ""
      local rest = name:sub(#prefix + 1)
      local number = tonumber(rest:match("^(%d+)%-")) or 0
      local track_guid, track_name, track_index, is_master
      if not master_only and number >= 1 and number <= track_count then
        local track = reaper.GetTrack(project, number - 1)
        track_guid = reaper.GetTrackGUID(track)
        local name_ok
        name_ok, track_name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
        if not name_ok then error("Could not read rendered track name") end
        track_index = number - 1
        if seen_tracks[track_guid] then error("Duplicate rendered track number") end
        seen_tracks[track_guid] = true
        seen_track_count = seen_track_count + 1
        is_master = false
      else
        master_count = master_count + 1
        track_guid = reaper.GetTrackGUID(reaper.GetMasterTrack(project))
        track_name = "MASTER"
        track_index = -1
        is_master = true
      end
      local file = io.open(target, "rb")
      if not file then error("REAPER did not create analysis audio: " .. target) end
      local bytes = file:seek("end") or 0
      file:close()
      if bytes <= 44 then error("REAPER created an empty or invalid WAV: " .. target) end
      files[#files + 1] = { source_guid = track_guid, name = track_name,
                            track_index = track_index, is_master = is_master,
                            path = target, bytes = bytes }
    end
    if master_count ~= 1 or (not master_only and seen_track_count ~= track_count) then
      error("Rendered files could not be mapped one-to-one to project tracks and master")
    end
    return { project_id = revision.project_id, revision = revision,
             files = files, capture_point = master_only and
               "native_render_master_only" or "native_render_track_stems_plus_master",
             render_action = action_name }
  end, function(err) return err end)

  local restore_failures = restore()
  if not ok then
    for _, target in ipairs(capture_targets) do
      local parent = target:match("^(.*)[/\\][^/\\]+$")
      if same_path(parent, output_dir) then os.remove(target) end
    end
    if type(capture) == "table" and capture.bridge_error then
      fail(capture.code, capture.message, capture.details)
    end
    fail("capture_failed", tostring(capture),
         { project_id = revision.project_id, output_dir = output_dir,
           restore_failures = restore_failures })
  end
  if #restore_failures > 0 then
    for _, target in ipairs(capture_targets) do
      local parent = target:match("^(.*)[/\\][^/\\]+$")
      if same_path(parent, output_dir) then os.remove(target) end
    end
    fail("capture_restore_failed", "Render completed but REAPER settings could not be restored",
         { output_dir = output_dir, restore_failures = restore_failures,
           files = capture.files })
  end
  capture.revision_before = revision
  capture.revision_after = project_identity(project, path)
  return capture
end

local function projects_list()
  local projects, index = json.array(), 0
  while true do
    local project, path = reaper.EnumProjects(index, "")
    if not project then break end
    local identity = project_identity(project, path or "")
    projects[#projects + 1] = {
      revision = identity,
      name = reaper.GetProjectName(project),
      is_active = select(1, reaper.EnumProjects(-1, "")) == project,
    }
    index = index + 1
  end
  return { projects = projects, count = #projects }
end

local function write_instance_registry()
  local active, active_path = active_project()
  local payload = {
    protocol = protocol_version,
    bridge_session_id = bridge_session_id,
    reaper_version = reaper.GetAppVersion(),
    started_epoch = tonumber(bridge_session_id:match("^(%x+)"), 16),
    last_seen_epoch = os.time(),
    active_project = project_identity(active, active_path),
    open_projects = projects_list().projects,
  }
  local temporary = registry_path .. ".tmp"
  local file, open_error = io.open(temporary, "wb")
  if not file then error("cannot write instance registry: " .. tostring(open_error)) end
  file:write(json.encode(payload))
  file:close()
  local ok, rename_error = os.rename(temporary, registry_path)
  if not ok then error("cannot publish instance registry: " .. tostring(rename_error)) end
end

local function write_heartbeat()
  local file = io.open(pulse_path, "r+b")
  if not file then file = io.open(pulse_path, "wb") end
  if not file then error("cannot update REAPER bridge heartbeat") end
  file:seek("set", 0)
  file:write(string.format("%010d", os.time()))
  file:close()
end

local function path_equal(left, right)
  if type(left) ~= "string" or type(right) ~= "string" then return false end
  local function normalized(path)
    return path:gsub("/", "\\"):gsub("\\+", "\\"):lower()
  end
  return normalized(left) == normalized(right)
end

local function project_save(arguments)
  local project, current_path = target_project(arguments)
  local mode = arguments.mode
  if mode == "current" then
    if current_path == "" then
      fail("validation_error", "Unsaved projects require an explicit save_as path")
    end
    local before = reaper.GetProjectStateChangeCount(project)
    reaper.Main_SaveProject(project, false)
    local after_path = project_path_for(project)
    local exists = io.open(current_path, "rb")
    local saved = exists ~= nil
    if exists then exists:close() end
    return { mode = mode, path = current_path, saved = saved,
             change_count_before = before,
             change_count_after = reaper.GetProjectStateChangeCount(project),
             project_path_after = after_path or json.null }
  elseif mode ~= "save_as" then
    fail("validation_error", "Save mode must be 'current' or 'save_as'")
  end

  local target = arguments.path
  if type(target) ~= "string" or target == "" then
    fail("validation_error", "save_as requires a non-empty absolute file path")
  end
  if not target:match("^[A-Za-z]:[/\\]") and not target:match("^[/\\][/\\]") then
    fail("validation_error", "save_as path must be absolute", { path = target })
  end
  local extension = target:match("%.([^./\\]+)$")
  if not extension or extension:lower() ~= "rpp" then
    fail("validation_error", "save_as path must end in .RPP", { path = target })
  end
  local existing = io.open(target, "rb")
  if existing then existing:close() end
  if existing and arguments.overwrite_existing ~= true then
    fail("validation_error", "Save target already exists; overwrite was not authorized",
         { path = target, current_path = current_path })
  end

  local before = reaper.GetProjectStateChangeCount(project)
  -- &8 makes the requested path the project's new filename. Do not use this
  -- route for checkpoints, which must not rebind the open project.
  reaper.Main_SaveProjectEx(project, target, 8)
  local after_path = project_path_for(project)
  local output = io.open(target, "rb")
  local saved = output ~= nil
  if output then output:close() end
  if not saved or not path_equal(after_path, target) then
    fail("transaction_failed", "REAPER did not confirm the requested save path",
         { requested_path = target, project_path_after = after_path,
           target_exists = saved })
  end
  return { mode = mode, path = after_path, saved = true,
           change_count_before = before,
           change_count_after = reaper.GetProjectStateChangeCount(project),
           project_id_after = project_identity(project, after_path).project_id }
end

local function action_list(arguments)
  local section_id = tonumber(arguments.section_id or 0)
  if not section_id or section_id % 1 ~= 0 then error("section_id must be an integer") end
  local section = reaper.SectionFromUniqueID(section_id)
  if not section then error("Unknown action section: " .. tostring(section_id)) end
  local query = tostring(arguments.query or ""):lower()
  local offset = math.max(0, math.floor(tonumber(arguments.offset) or 0))
  local limit = math.max(1, math.min(5000, math.floor(tonumber(arguments.limit) or 500)))
  local actions, matched, index = json.array(), 0, 0
  while true do
    local command_id, name = reaper.kbd_enumerateActions(section, index)
    if not command_id or command_id == 0 then break end
    name = name or ""
    if query == "" or name:lower():find(query, 1, true) then
      if matched >= offset and #actions < limit then
        actions[#actions + 1] = { command_id = command_id, name = name }
      end
      matched = matched + 1
    end
    index = index + 1
  end
  return { section_id = section_id, query = query, offset = offset,
           total_matches = matched, actions = actions,
           next_offset = offset + #actions < matched and offset + #actions or json.null }
end

local function action_invoke(arguments)
  local project = active_project()
  local section_id = tonumber(arguments.section_id or 0)
  local command = arguments.command_id
  if type(command) == "string" then command = reaper.NamedCommandLookup(command) end
  command = tonumber(command)
  if not command or command == 0 then error("Unknown action command") end
  local before = reaper.GetProjectStateChangeCount(project)
  if section_id == 0 then
    reaper.Main_OnCommandEx(command, 0, project)
  elseif section_id == 32060 then
    local editor = reaper.MIDIEditor_GetActive()
    if not editor then error("MIDI Editor action requires an active MIDI editor") end
    reaper.MIDIEditor_OnCommand(editor, command)
  else
    error("Action execution is not yet mapped for this section; use section 0 or 32060")
  end
  local after = reaper.GetProjectStateChangeCount(project)
  return { section_id = section_id, command_id = command,
           changed_project_state = before ~= after,
           change_count_before = before, change_count_after = after,
           note = "Low-level action invoked; inspect resulting project context." }
end

local function json_value(value, depth, seen)
  if depth > 64 then fail("api_call_failed", "API result nesting limit exceeded") end
  if value == nil or value == json.null then return json.null end
  local kind = type(value)
  if kind == "boolean" or kind == "string" then return value end
  if kind == "number" then
    if not finite_number(value) then
      fail("api_call_failed", "API returned a non-finite number")
    end
    return value
  end
  if kind ~= "table" then
    fail("api_call_failed", "API returned a value that cannot be encoded as JSON",
         { return_type = kind })
  end
  seen = seen or {}
  if seen[value] then fail("api_call_failed", "API returned a cyclic table") end
  seen[value] = true
  local is_array, count, highest = getmetatable(value) == json.array_mt, 0, 0
  if not is_array then
    is_array = true
    for key in pairs(value) do
      if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then
        is_array = false
        break
      end
      count = count + 1
      highest = math.max(highest, key)
    end
    is_array = is_array and count == highest
  end
  local output = is_array and json.array() or {}
  for key, item in pairs(value) do
    local converted = json_value(item, depth + 1, seen)
    if is_array then output[key] = converted else
      if type(key) ~= "string" then
        fail("api_call_failed", "API returned a table with non-string object keys")
      end
      output[key] = converted
    end
  end
  seen[value] = nil
  return output
end

local function api_call(arguments, request_id)
  local name = arguments.name
  if type(name) ~= "string" or not name:match("^[%a_][%w_]*$") then
    fail("validation_error", "API name must be a valid function identifier")
  end
  if not reaper.APIExists(name) then
    fail("api_unavailable", "API function is not available",
         { name = name, version = reaper.GetAppVersion() })
  end
  local fn = reaper[name]
  if type(fn) ~= "function" then error("API function is not callable: " .. name) end
  local args = arguments.args or json.array()
  local parameter_types = arguments.parameter_types
  local return_types = arguments.return_types
  if type(args) ~= "table" or type(parameter_types) ~= "table" or
     type(return_types) ~= "table" or #args ~= #parameter_types then
    fail("validation_error", "API arguments do not match the supplied signature",
         { name = name, argument_count = type(args) == "table" and #args or -1,
           parameter_count = type(parameter_types) == "table" and #parameter_types or -1 })
  end

  local call_args = { n = #args }
  local arrays = {}
  local call_project, call_path = active_project()
  local call_project_id = project_identity(call_project, call_path).project_id
  local has_project_handle = false
  for index = 1, #args do
    local value = args[index]
    local parameter = parameter_types[index]
    if type(parameter) ~= "table" or type(parameter.type) ~= "string" or
       type(parameter.host_handle) ~= "boolean" then
      fail("validation_error", "Invalid API parameter metadata", { name = name, index = index })
    end
    if value == json.null then
      if parameter.optional ~= true then
        fail("validation_error", "Null passed for a required API argument",
             { name = name, index = index, type = parameter.type })
      end
      call_args[index] = nil
    elseif parameter.host_handle then
      local pointer, project, path = resolve_api_handle(value, parameter.type)
      local resolved_project_id = project_identity(project, path).project_id
      if has_project_handle and call_project_id ~= resolved_project_id then
        fail("stale_context", "API call arguments refer to different projects",
             { expected_project_id = call_project_id,
               received_project_id = resolved_project_id })
      end
      call_project, call_path, call_project_id = project, path, resolved_project_id
      has_project_handle = true
      call_args[index] = pointer
    elseif parameter.type == "reaper.array" then
      local values = type(value) == "table" and value["$array"] or nil
      if type(values) ~= "table" then
        fail("validation_error", "reaper.array arguments must use the typed array wrapper",
             { name = name, index = index })
      end
      local array = reaper.new_array(#values)
      for sample_index = 1, #values do
        if not finite_number(values[sample_index]) then
          fail("validation_error", "reaper.array contains a non-finite or non-numeric value",
               { name = name, index = index, sample_index = sample_index })
        end
        array[sample_index] = values[sample_index]
      end
      arrays[index] = array
      call_args[index] = array
    else
      if type(value) == "table" or type(value) == "userdata" then
        fail("validation_error", "API scalar argument has an unsupported value type",
             { name = name, index = index, expected_type = parameter.type })
      end
      if type(value) == "number" and not finite_number(value) then
        fail("validation_error", "API arguments cannot contain non-finite numbers",
             { name = name, index = index })
      end
      call_args[index] = value
    end
  end

  local before_path = project_path_for(call_project)
  local before = before_path and project_identity(call_project, before_path) or nil
  local packed = table.pack(pcall(fn, table.unpack(call_args, 1, call_args.n)))
  if not packed[1] then fail("api_call_failed", tostring(packed[2]), { name = name }) end
  local after_path = project_path_for(call_project)
  local after = after_path and project_identity(call_project, after_path) or json.null
  local returns = json.array()
  for index = 2, packed.n do
    local value = packed[index]
    local return_index = index - 1
    local return_type = return_types[return_index]
    if type(value) == "userdata" and return_type and return_type.host_handle then
      value = remember_api_handle(value, return_type.type, call_project, call_path,
                                  nil, request_id, return_index)
    elseif type(value) == "userdata" and return_type and return_type.type == "reaper.array" then
      local array_values = value.table()
      for sample_index = 1, #array_values do
        if not finite_number(array_values[sample_index]) then
          fail("api_call_failed", "API returned a non-finite array value",
               { name = name, return_index = return_index,
                 sample_index = sample_index })
        end
      end
      value = json.array(array_values)
    elseif type(value) == "table" then
      value = json_value(value, 0)
    elseif type(value) == "number" and not finite_number(value) then
      fail("api_call_failed", "API returned a non-finite number",
           { name = name, return_index = return_index })
    elseif type(value) == "userdata" or type(value) == "function" or type(value) == "thread" then
      fail("api_call_failed", "API returned an opaque object without a typed ReaScript signature",
           { name = name, return_index = return_index,
             return_type = return_type and return_type.type or type(value) })
    elseif value == nil then
      value = json.null
    end
    returns[#returns + 1] = value
  end

  local mutated_arrays = {}
  for index, array in pairs(arrays) do
    mutated_arrays[tostring(index)] = json.array(array.table())
  end
  return { name = name, returns = returns, arrays = mutated_arrays,
           project_before = before or json.null, project_after = after,
           bridge_session = bridge_session_id }
end

local function assert_revision(project, path, expected)
  if type(expected) ~= "table" or type(expected.project_id) ~= "string" or
     type(expected.path) ~= "string" or not finite_number(expected.change_count) then
    fail("validation_error", "A complete project revision is required")
  end
  local current = project_identity(project, path)
  if current.project_id ~= expected.project_id or current.path ~= expected.path or
     current.change_count ~= expected.change_count then
    fail("stale_context", "The project changed after the edit plan was prepared",
         { expected = expected, current = current })
  end
  return current
end

local function require_object_values(values, allowed, required)
  if type(values) ~= "table" then
    fail("validation_error", "operation.values must be an object")
  end
  local count = 0
  for key in pairs(values) do
    count = count + 1
    if type(key) ~= "string" or not allowed[key] then
      fail("validation_error", "Unsupported edit property", { property = key })
    end
  end
  if count == 0 then fail("validation_error", "An edit operation has no values") end
  for key in pairs(required or {}) do
    if values[key] == nil then
      fail("validation_error", "Required edit property is missing", { property = key })
    end
  end
end

local function validate_track_values(values)
  local allowed = { name = true, volume = true, pan = true, mute = true,
                    record_arm = true, record_mode = true,
                    record_mode_flags = true, main_send = true,
                    selected = true }
  require_object_values(values, allowed)
  for key, value in pairs(values) do
    if key == "name" then
      if type(value) ~= "string" then
        fail("validation_error", "track name must be a string")
      end
    elseif key == "volume" then
      if not finite_number(value) or value < 0 then
        fail("validation_error", "track volume must be a finite non-negative linear value")
      end
    elseif key == "pan" then
      if not finite_number(value) or value < -1 or value > 1 then
        fail("validation_error", "track pan must be between -1 and 1")
      end
    elseif key == "record_mode" then
      if not finite_number(value) or value % 1 ~= 0 or value < 0 or value > 8 then
        fail("validation_error", "track record_mode must be an integer from 0 to 8")
      end
    elseif key == "record_mode_flags" then
      if not finite_number(value) or value % 1 ~= 0 or value < 0 or
         value > 4294967295 or (value & 3) == 3 then
        fail("validation_error", "track record_mode_flags must be a valid non-negative bit field")
      end
    elseif type(value) ~= "boolean" then
      fail("validation_error", "track mute/arm/main_send values must be booleans",
           { property = key })
    end
  end
end

local function validate_item_values(values)
  local allowed = { position_s = true, length_s = true, mute = true,
                    stretch_ratio = true, preserve_pitch = true,
                    stretch_markers = true, beat_attachmode = true,
                    auto_stretch = true }
  require_object_values(values, allowed)
  if values.position_s ~= nil and not finite_number(values.position_s) then
    fail("validation_error", "item position_s must be finite")
  end
  if values.length_s ~= nil and
     (not finite_number(values.length_s) or values.length_s <= 0) then
    fail("validation_error", "item length_s must be finite and positive")
  end
  if values.mute ~= nil and type(values.mute) ~= "boolean" then
    fail("validation_error", "item mute must be a boolean")
  end
  if values.stretch_ratio ~= nil and
     (not finite_number(values.stretch_ratio) or values.stretch_ratio < 0.5 or
      values.stretch_ratio > 2.0) then
    fail("validation_error", "item stretch_ratio must be between 0.5 and 2")
  end
  if values.preserve_pitch ~= nil and type(values.preserve_pitch) ~= "boolean" then
    fail("validation_error", "item preserve_pitch must be a boolean")
  end
  if values.stretch_markers ~= nil then
    if type(values.stretch_markers) ~= "table" or #values.stretch_markers > 256 then
      fail("validation_error", "item stretch_markers must be an array of at most 256 entries")
    end
    local previous = -1
    for index, marker in ipairs(values.stretch_markers) do
      if type(marker) ~= "table" or not finite_number(marker.position_s) or
         marker.position_s < 0 or marker.position_s <= previous then
        fail("validation_error", "each stretch marker needs a strictly increasing position_s",
             { marker_index = index })
      end
      if marker.source_position_s ~= nil and
         (not finite_number(marker.source_position_s) or marker.source_position_s < 0) then
        fail("validation_error", "stretch marker source_position_s must be non-negative",
             { marker_index = index })
      end
      previous = marker.position_s
    end
  end
  if values.beat_attachmode ~= nil and
     (not finite_number(values.beat_attachmode) or values.beat_attachmode % 1 ~= 0 or
      values.beat_attachmode < -1 or values.beat_attachmode > 2) then
    fail("validation_error", "item beat_attachmode must be an integer from -1 to 2")
  end
  if values.auto_stretch ~= nil and type(values.auto_stretch) ~= "boolean" then
    fail("validation_error", "item auto_stretch must be a boolean")
  end
end

local function validate_midi_create_values(values)
  require_object_values(values, { position_s = true, length_s = true, notes = true },
                        { position_s = true, length_s = true, notes = true })
  if not finite_number(values.position_s) or not finite_number(values.length_s) or
     values.length_s <= 0 then
    fail("validation_error", "midi.create requires a finite position and positive length")
  end
  if type(values.notes) ~= "table" or #values.notes > 10000 then
    fail("validation_error", "midi.create notes must be an array of at most 10000 events")
  end
  for key in pairs(values.notes) do
    if type(key) ~= "number" or key < 1 or key % 1 ~= 0 or key > #values.notes then
      fail("validation_error", "midi.create notes must be a dense array")
    end
  end
  local allowed_note_keys = { start_s = true, end_s = true, pitch = true,
                              velocity = true, channel = true, muted = true }
  for index, note in ipairs(values.notes) do
    if type(note) ~= "table" then
      fail("validation_error", "Each MIDI note must be an object", { note_index = index - 1 })
    end
    for key in pairs(note) do
      if not allowed_note_keys[key] then
        fail("validation_error", "Unknown MIDI note field", { note_index = index - 1,
             field = tostring(key) })
      end
    end
    if not finite_number(note.start_s) or not finite_number(note.end_s) or
       note.start_s < 0 or note.end_s <= note.start_s or note.end_s > values.length_s then
      fail("validation_error", "MIDI note times must be relative to the item and fit its length",
           { note_index = index - 1, item_length_s = values.length_s })
    end
    if not finite_number(note.pitch) or note.pitch % 1 ~= 0 or
       note.pitch < 0 or note.pitch > 127 then
      fail("validation_error", "MIDI pitch must be an integer from 0 to 127",
           { note_index = index - 1 })
    end
    if not finite_number(note.velocity) or note.velocity % 1 ~= 0 or
       note.velocity < 1 or note.velocity > 127 then
      fail("validation_error", "MIDI velocity must be an integer from 1 to 127",
           { note_index = index - 1 })
    end
    if note.channel == nil then note.channel = 0 end
    if not finite_number(note.channel) or note.channel % 1 ~= 0 or
       note.channel < 0 or note.channel > 15 then
      fail("validation_error", "MIDI channel must be an integer from 0 to 15",
           { note_index = index - 1 })
    end
    if note.muted == nil then note.muted = false end
    if type(note.muted) ~= "boolean" then
      fail("validation_error", "MIDI muted must be a boolean", { note_index = index - 1 })
    end
  end
end

local function find_fx_by_guid(track, fx_guid)
  if type(fx_guid) ~= "string" or fx_guid == "" then return nil end
  for index = 0, reaper.TrackFX_GetCount(track) - 1 do
    if reaper.TrackFX_GetFXGUID(track, index) == fx_guid then return index end
  end
  return nil
end

local function validate_fx_values(kind, values)
  if kind == "fx.set_enabled" then
    require_object_values(values, { fx_guid = true, enabled = true },
                          { fx_guid = true, enabled = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" or
       type(values.enabled) ~= "boolean" then
      fail("validation_error", "fx.set_enabled requires fx_guid and boolean enabled")
    end
  elseif kind == "fx.insert" then
    require_object_values(values, { name = true, ident = true, position = true },
                          {})
    if (type(values.name) ~= "string" or values.name == "") and
       (type(values.ident) ~= "string" or values.ident == "") then
      fail("validation_error", "fx.insert requires a plugin name or ident")
    end
    if values.position ~= nil and (not finite_number(values.position) or
       values.position % 1 ~= 0 or values.position < 0) then
      fail("validation_error", "fx.insert position must be a non-negative integer")
    end
  elseif kind == "fx.delete" then
    require_object_values(values, { fx_guid = true }, { fx_guid = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" then
      fail("validation_error", "fx.delete requires fx_guid")
    end
  elseif kind == "fx.get_chunk" then
    require_object_values(values, { fx_guid = true }, { fx_guid = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" then
      fail("validation_error", "fx.get_chunk requires fx_guid")
    end
  elseif kind == "fx.set_preset" then
    require_object_values(values, { fx_guid = true, preset = true },
                          { fx_guid = true, preset = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" or
       type(values.preset) ~= "string" or values.preset == "" then
      fail("validation_error", "fx.set_preset requires fx_guid and a preset name")
    end
  elseif kind == "fx.move" then
    require_object_values(values, { fx_guid = true, to_index = true },
                          { fx_guid = true, to_index = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" or
       not finite_number(values.to_index) or values.to_index % 1 ~= 0 or
       values.to_index < 0 then
      fail("validation_error", "fx.move requires fx_guid and a non-negative integer to_index")
    end
  elseif kind == "fx.offline" then
    require_object_values(values, { fx_guid = true, offline = true },
                          { fx_guid = true, offline = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" or
       type(values.offline) ~= "boolean" then
      fail("validation_error", "fx.offline requires fx_guid and boolean offline")
    end
  else
    require_object_values(values, { fx_guid = true, parameter_index = true,
                                    normalized = true },
                          { fx_guid = true, parameter_index = true,
                            normalized = true })
    if type(values.fx_guid) ~= "string" or values.fx_guid == "" or
       not finite_number(values.parameter_index) or values.parameter_index < 0 or
       values.parameter_index % 1 ~= 0 or not finite_number(values.normalized) or
       values.normalized < 0 or values.normalized > 1 then
      fail("validation_error", "fx.set_parameter requires a GUID, integer parameter index, and normalized value in [0, 1]")
    end
  end
end

local function validate_track_create_values(values)
  require_object_values(values, { name = true, position = true }, { name = true })
  if type(values.name) ~= "string" or values.name == "" or #values.name > 256 then
    fail("validation_error", "track.create requires a non-empty name")
  end
  if values.position ~= nil and (not finite_number(values.position) or
     values.position % 1 ~= 0 or values.position < 0) then
    fail("validation_error", "track.create position must be a non-negative integer")
  end
end

local function validate_item_create_values(values)
  require_object_values(values, { position_s = true, length_s = true, file = true },
                        { position_s = true, length_s = true })
  if not finite_number(values.position_s) or values.position_s < 0 or
     not finite_number(values.length_s) or values.length_s <= 0 then
    fail("validation_error", "item.create requires a non-negative position and positive length")
  end
  if values.file ~= nil and (type(values.file) ~= "string" or values.file == "") then
    fail("validation_error", "item.create file must be a non-empty path")
  end
end

local function validate_delete_values(kind, values)
  require_object_values(values, { confirm = true }, { confirm = true })
  if values.confirm ~= true then
    fail("validation_error", kind .. " requires confirm=true")
  end
end

local function validate_item_split_values(values)
  require_object_values(values, { position_s = true, fade_s = true, tail_s = true,
                                  right_trim_s = true, move_s = true },
                        { position_s = true })
  if not finite_number(values.position_s) then
    fail("validation_error", "item.split requires a finite position_s")
  end
  if values.fade_s ~= nil and (not finite_number(values.fade_s) or
     values.fade_s < 0 or values.fade_s > 1) then
    fail("validation_error", "item.split fade_s must be between 0 and 1 second")
  end
  if values.tail_s ~= nil and (not finite_number(values.tail_s) or
     values.tail_s < 0 or values.tail_s > 2) then
    fail("validation_error", "item.split tail_s must be between 0 and 2 seconds")
  end
  if values.right_trim_s ~= nil and (not finite_number(values.right_trim_s) or
     values.right_trim_s < 0 or values.right_trim_s > 2) then
    fail("validation_error", "item.split right_trim_s must be between 0 and 2 seconds")
  end
  if values.move_s ~= nil and (not finite_number(values.move_s) or
     math.abs(values.move_s) > 2) then
    fail("validation_error", "item.split move_s must be between -2 and 2 seconds")
  end
end

local function validate_envelope_values(values)
  require_object_values(values, { envelope = true, points = true, clear = true },
                        { envelope = true, points = true })
  if type(values.envelope) ~= "string" or values.envelope == "" then
    fail("validation_error", "envelope.set requires an envelope name")
  end
  if type(values.points) ~= "table" or #values.points > 10000 then
    fail("validation_error", "envelope.set points must be an array of at most 10000 points")
  end
  for key in pairs(values.points) do
    if type(key) ~= "number" or key < 1 or key % 1 ~= 0 or key > #values.points then
      fail("validation_error", "envelope.set points must be a dense array")
    end
  end
  for index, point in ipairs(values.points) do
    if type(point) ~= "table" then
      fail("validation_error", "Each envelope point must be an object",
           { point_index = index - 1 })
    end
    for key in pairs(point) do
      if key ~= "time_s" and key ~= "value" and key ~= "shape" and
         key ~= "tension" and key ~= "selected" then
        fail("validation_error", "Unknown envelope point field",
             { point_index = index - 1, field = tostring(key) })
      end
    end
    if not finite_number(point.time_s) or point.time_s < 0 then
      fail("validation_error", "Envelope point time_s must be finite and non-negative",
           { point_index = index - 1 })
    end
    if not finite_number(point.value) then
      fail("validation_error", "Envelope point value must be finite",
           { point_index = index - 1 })
    end
    if point.shape ~= nil and (not finite_number(point.shape) or
       point.shape % 1 ~= 0 or point.shape < 0 or point.shape > 5) then
      fail("validation_error", "Envelope point shape must be an integer from 0 to 5",
           { point_index = index - 1 })
    end
    if point.tension ~= nil and (not finite_number(point.tension) or
       point.tension < -1 or point.tension > 1) then
      fail("validation_error", "Envelope point tension must be between -1 and 1",
           { point_index = index - 1 })
    end
    if point.selected ~= nil and type(point.selected) ~= "boolean" then
      fail("validation_error", "Envelope point selected must be a boolean",
           { point_index = index - 1 })
    end
  end
  if values.clear ~= nil and type(values.clear) ~= "boolean" then
    fail("validation_error", "envelope.set clear must be a boolean")
  end
end

local function validate_tempo_values(values)
  require_object_values(values, { bpm = true, numerator = true, denominator = true,
                                  time_s = true, beat = true, remove = true },
                        { bpm = true })
  if not finite_number(values.bpm) or values.bpm <= 0 or values.bpm > 960 then
    fail("validation_error", "tempo.set bpm must be greater than 0 and at most 960")
  end
  if values.time_s ~= nil and (not finite_number(values.time_s) or values.time_s < 0) then
    fail("validation_error", "tempo.set time_s must be non-negative")
  end
  if values.beat ~= nil and (not finite_number(values.beat) or values.beat < 0) then
    fail("validation_error", "tempo.set beat must be non-negative")
  end
  if values.remove ~= nil and type(values.remove) ~= "boolean" then
    fail("validation_error", "tempo.set remove must be a boolean")
  end
  if values.numerator ~= nil and (not finite_number(values.numerator) or
     values.numerator % 1 ~= 0 or values.numerator < 1 or values.numerator > 64) then
    fail("validation_error", "tempo.set numerator must be an integer from 1 to 64")
  end
  if values.denominator ~= nil and (not finite_number(values.denominator) or
     values.denominator % 1 ~= 0 or values.denominator < 1 or values.denominator > 64) then
    fail("validation_error", "tempo.set denominator must be an integer from 1 to 64")
  end
  if (values.numerator == nil) ~= (values.denominator == nil) then
    fail("validation_error", "tempo.set numerator and denominator must be provided together")
  end
end

local function validate_region_delete_values(values)
  require_object_values(values, { marker_index = true, is_region = true },
                        { marker_index = true })
  if not finite_number(values.marker_index) or values.marker_index % 1 ~= 0 or
     values.marker_index < 0 then
    fail("validation_error", "region.delete requires a non-negative integer marker_index")
  end
  if values.is_region ~= nil and type(values.is_region) ~= "boolean" then
    fail("validation_error", "region.delete is_region must be a boolean")
  end
end

local function validate_track_reorder_values(values)
  require_object_values(values, { before_index = true }, { before_index = true })
  if not finite_number(values.before_index) or values.before_index % 1 ~= 0 or
     values.before_index < 0 then
    fail("validation_error", "track.reorder requires a non-negative integer before_index")
  end
end

local function validate_item_duplicate_values(values)
  require_object_values(values, { offset_s = true, copies = true },
                        { offset_s = true })
  if not finite_number(values.offset_s) or values.offset_s == 0 then
    fail("validation_error", "item.duplicate requires a non-zero finite offset_s")
  end
  if values.copies ~= nil and (not finite_number(values.copies) or
     values.copies % 1 ~= 0 or values.copies < 1 or values.copies > 64) then
    fail("validation_error", "item.duplicate copies must be an integer from 1 to 64")
  end
end

local function validate_marker_create_values(values)
  require_object_values(values, { position_s = true, name = true,
                                  region_length_s = true, color = true },
                        { position_s = true, name = true })
  if not finite_number(values.position_s) or values.position_s < 0 then
    fail("validation_error", "marker.create requires a non-negative position_s")
  end
  if type(values.name) ~= "string" or values.name == "" or #values.name > 256 then
    fail("validation_error", "marker.create requires a non-empty name")
  end
  if values.region_length_s ~= nil and (not finite_number(values.region_length_s) or
     values.region_length_s <= 0) then
    fail("validation_error", "marker.create region_length_s must be positive")
  end
  if values.color ~= nil and (not finite_number(values.color) or values.color < 0 or
     values.color > 16777215 or values.color % 1 ~= 0) then
    fail("validation_error", "marker.create color must be a 24-bit integer")
  end
end

local function validate_send_create_values(values)
  require_object_values(values, { destination_guid = true, volume = true,
                                  pan = true, mute = true },
                        { destination_guid = true })
  if type(values.destination_guid) ~= "string" or values.destination_guid == "" then
    fail("validation_error", "send.create requires a destination track GUID")
  end
  if values.volume ~= nil and (not finite_number(values.volume) or values.volume < 0) then
    fail("validation_error", "send volume must be a finite non-negative linear value")
  end
  if values.pan ~= nil and (not finite_number(values.pan) or
     values.pan < -1 or values.pan > 1) then
    fail("validation_error", "send pan must be between -1 and 1")
  end
  if values.mute ~= nil and type(values.mute) ~= "boolean" then
    fail("validation_error", "send mute must be a boolean")
  end
end

local function validate_send_delete_values(values)
  require_object_values(values, { send_index = true }, { send_index = true })
  if not finite_number(values.send_index) or values.send_index % 1 ~= 0 or
     values.send_index < 0 then
    fail("validation_error", "send.delete requires a non-negative integer send_index")
  end
end

local function validate_take_values(values)
  require_object_values(values, { gain = true, pan = true, pitch = true,
                                  playrate = true })
  if values.gain ~= nil and (not finite_number(values.gain) or values.gain < 0) then
    fail("validation_error", "take gain must be a finite non-negative linear value")
  end
  if values.pan ~= nil and (not finite_number(values.pan) or
     values.pan < -1 or values.pan > 1) then
    fail("validation_error", "take pan must be between -1 and 1")
  end
  if values.pitch ~= nil and (not finite_number(values.pitch) or
     values.pitch < -48 or values.pitch > 48) then
    fail("validation_error", "take pitch must be between -48 and 48 semitones")
  end
  if values.playrate ~= nil and (not finite_number(values.playrate) or
     values.playrate <= 0 or values.playrate > 4) then
    fail("validation_error", "take playrate must be greater than 0 and at most 4")
  end
end

local function validate_send_values(values)
  require_object_values(values, { send_index = true, volume = true, pan = true,
                                  mute = true }, { send_index = true })
  if not finite_number(values.send_index) or values.send_index % 1 ~= 0 or
     values.send_index < 0 then
    fail("validation_error", "send.set requires a non-negative integer send_index")
  end
  if values.volume ~= nil and (not finite_number(values.volume) or values.volume < 0) then
    fail("validation_error", "send volume must be a finite non-negative linear value")
  end
  if values.pan ~= nil and (not finite_number(values.pan) or
     values.pan < -1 or values.pan > 1) then
    fail("validation_error", "send pan must be between -1 and 1")
  end
  if values.mute ~= nil and type(values.mute) ~= "boolean" then
    fail("validation_error", "send mute must be a boolean")
  end
  if values.volume == nil and values.pan == nil and values.mute == nil then
    fail("validation_error", "send.set requires volume, pan, or mute")
  end
end

local function normalize_edit_operation(project, operation)
  if type(operation) ~= "table" or type(operation.kind) ~= "string" then
    fail("validation_error", "Each edit operation needs a kind")
  end
  local kind = operation.kind
  local target_guid = operation.target_guid
  if type(target_guid) ~= "string" or target_guid == "" then
    fail("validation_error", "Each edit operation needs a target GUID",
         { kind = kind })
  end
  local values = operation.values
  local normalized = { kind = kind, target_guid = target_guid, values = {} }
  if kind == "track.set" then
    validate_track_values(values)
  elseif kind == "item.set" then
    validate_item_values(values)
  elseif kind == "midi.create" then
    validate_midi_create_values(values)
  elseif kind == "fx.set_enabled" or kind == "fx.set_parameter" or
         kind == "fx.insert" or kind == "fx.delete" or
         kind == "fx.get_chunk" or
         kind == "fx.set_preset" or kind == "fx.move" or
         kind == "fx.offline" then
    validate_fx_values(kind, values)
  elseif kind == "send.set" then
    validate_send_values(values)
  elseif kind == "send.create" then
    validate_send_create_values(values)
  elseif kind == "send.delete" then
    validate_send_delete_values(values)
  elseif kind == "take.set" then
    validate_take_values(values)
  elseif kind == "track.create" then
    validate_track_create_values(values)
  elseif kind == "item.create" then
    validate_item_create_values(values)
  elseif kind == "item.split" then
    validate_item_split_values(values)
  elseif kind == "item.duplicate" then
    validate_item_duplicate_values(values)
  elseif kind == "envelope.set" then
    validate_envelope_values(values)
  elseif kind == "tempo.set" then
    validate_tempo_values(values)
  elseif kind == "region.delete" then
    validate_region_delete_values(values)
  elseif kind == "track.reorder" then
    validate_track_reorder_values(values)
  elseif kind == "marker.create" then
    validate_marker_create_values(values)
  elseif kind == "track.delete" or kind == "item.delete" then
    validate_delete_values(kind, values)
  else
    fail("validation_error", "Unsupported transactional edit operation",
         { kind = kind, supported = { "track.set", "track.create",
                                       "track.delete", "track.reorder",
                                       "item.set", "item.create", "item.delete",
                                       "item.split", "item.duplicate",
                                       "take.set", "midi.create",
                                       "fx.set_enabled", "fx.set_parameter",
                                       "fx.insert", "fx.delete", "fx.move",
                                       "fx.get_chunk",
                                       "fx.set_preset", "fx.offline",
                                       "send.set", "send.create", "send.delete",
                                       "marker.create", "region.delete",
                                       "envelope.set", "tempo.set" } })
  end
  for key, value in pairs(values) do normalized.values[key] = value end

  local target
  if kind == "item.set" or kind == "item.delete" or kind == "item.split" or
     kind == "item.duplicate" or kind == "take.set" then
    target = resolve_item(project, target_guid)
  elseif kind == "track.create" or kind == "marker.create" or kind == "tempo.set" or
         kind == "region.delete" then
    if target_guid ~= "project" then
      fail("validation_error", kind .. " targets the project; use target_guid=project")
    end
    target = project
  else
    target = resolve_track(project, target_guid)
  end
  if not target then
    fail("stale_context", "The edit target no longer exists",
         { kind = kind, target_guid = target_guid })
  end
  if kind == "send.create" then
    local destination = resolve_track(project, values.destination_guid)
    if not destination then
      fail("stale_context", "The send destination no longer exists",
           { destination_guid = values.destination_guid })
    end
    if destination == target then
      fail("validation_error", "A send destination must differ from its source",
           { target_guid = target_guid })
    end
  end
  if kind == "fx.set_enabled" or kind == "fx.set_parameter" or
     kind == "fx.set_preset" or kind == "fx.move" or kind == "fx.offline" then
    local fx_index = find_fx_by_guid(target, values.fx_guid)
    if not fx_index then
      fail("stale_context", "The FX target no longer exists",
           { track_guid = target_guid, fx_guid = values.fx_guid })
    end
    if kind == "fx.set_parameter" and
       values.parameter_index >= reaper.TrackFX_GetNumParams(target, fx_index) then
      fail("validation_error", "FX parameter index is outside the plugin parameter range",
           { fx_guid = values.fx_guid,
             parameter_index = values.parameter_index,
             parameter_count = reaper.TrackFX_GetNumParams(target, fx_index) })
    end
    if kind == "fx.move" and
       values.to_index > reaper.TrackFX_GetCount(target) then
      fail("validation_error", "FX move target is outside the chain",
           { to_index = values.to_index,
             fx_count = reaper.TrackFX_GetCount(target) })
    end
  end
  return normalized
end

local function find_envelope(track, name)
  if type(name) ~= "string" or name == "" then return nil end
  local envelope = reaper.GetTrackEnvelopeByName(track, name)
  if envelope then return envelope end
  local chunk_names = { Volume = "<VOLENV2", Pan = "<PANENV2", Mute = "<MUTEENV" }
  local chunk = chunk_names[name]
  if chunk then
    return reaper.GetTrackEnvelopeByChunkName(track, chunk)
  end
  return nil
end

local function read_edit_state(project, operation)
  local kind, values = operation.kind, operation.values
  local target
  if kind == "item.set" or kind == "item.delete" or kind == "item.split" or
     kind == "item.duplicate" or kind == "take.set" then
    target = resolve_item(project, operation.target_guid)
  elseif kind == "track.create" or kind == "marker.create" or kind == "tempo.set" or
         kind == "region.delete" then
    target = project
  else
    target = resolve_track(project, operation.target_guid)
  end
  if not target then
    fail("stale_context", "The edit target no longer exists",
         { kind = kind, target_guid = operation.target_guid })
  end
  local state = {}
  if kind == "track.set" then
    local fields = { volume = "D_VOL", pan = "D_PAN", mute = "B_MUTE",
                     record_arm = "I_RECARM", record_mode = "I_RECMODE",
                     record_mode_flags = "I_RECMODE_FLAGS",
                     main_send = "B_MAINSEND" }
    for key in pairs(values) do
      if key == "name" then state.name = get_track_name(target)
      elseif key == "selected" then state.selected = reaper.IsTrackSelected(target)
      elseif key == "mute" or key == "record_arm" or key == "main_send" then
        state[key] = reaper.GetMediaTrackInfo_Value(target, fields[key]) ~= 0
      else
        state[key] = reaper.GetMediaTrackInfo_Value(target, fields[key])
      end
    end
  elseif kind == "item.set" then
    local fields = { position_s = "D_POSITION", length_s = "D_LENGTH", mute = "B_MUTE",
                     beat_attachmode = "C_BEATATTACHMODE", auto_stretch = "C_AUTOSTRETCH" }
    local take = reaper.GetActiveTake(target)
    for key in pairs(values) do
      if key == "stretch_ratio" then
        state[key] = take and (1.0 / reaper.GetMediaItemTakeInfo_Value(
          take, "D_PLAYRATE")) or 1.0
      elseif key == "stretch_markers" then
        state[key] = take and reaper.GetTakeNumStretchMarkers(take) or 0
      elseif key == "preserve_pitch" then
        state[key] = take and reaper.GetMediaItemTakeInfo_Value(
          take, "B_PPITCH") ~= 0 or false
      else
        local value = reaper.GetMediaItemInfo_Value(target, fields[key])
        if key == "mute" then state[key] = value ~= 0 else state[key] = value end
      end
    end
  elseif kind == "midi.create" then
    state.item_count = reaper.CountTrackMediaItems(target)
    state.position_s = values.position_s
    state.length_s = values.length_s
    state.note_count = #values.notes
  elseif kind == "fx.insert" then
    state.fx_count = reaper.TrackFX_GetCount(target)
  elseif kind == "fx.delete" then
    local fx_index = find_fx_by_guid(target, values.fx_guid)
    if not fx_index then
      fail("stale_context", "The FX target no longer exists",
           { track_guid = operation.target_guid, fx_guid = values.fx_guid })
    end
    local name_ok, fx_name = reaper.TrackFX_GetFXName(target, fx_index)
    state.fx_guid = values.fx_guid
    state.fx_index = fx_index
    state.fx_name = name_ok and fx_name or ""
  elseif kind == "send.set" then
    local send_count = reaper.GetTrackNumSends(target, 0)
    if values.send_index >= send_count then
      fail("stale_context", "The send target no longer exists",
           { track_guid = operation.target_guid, send_index = values.send_index,
             send_count = send_count })
    end
    state.send_index = values.send_index
    state.volume = reaper.GetTrackSendInfo_Value(target, 0, values.send_index, "D_VOL")
    state.pan = reaper.GetTrackSendInfo_Value(target, 0, values.send_index, "D_PAN")
    state.mute = reaper.GetTrackSendInfo_Value(target, 0, values.send_index, "B_MUTE") ~= 0
  elseif kind == "track.create" then
    state.track_count = reaper.CountTracks(project)
  elseif kind == "track.delete" then
    state.track_name = get_track_name(target)
  elseif kind == "item.create" then
    state.item_count = reaper.CountTrackMediaItems(target)
  elseif kind == "item.split" then
    local position = reaper.GetMediaItemInfo_Value(target, "D_POSITION")
    local length = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
    if values.position_s <= position or values.position_s >= position + length then
      fail("validation_error", "item.split position must be strictly inside the item",
           { item_position_s = position, item_length_s = length,
             split_position_s = values.position_s })
    end
    state.position_s = position
    state.length_s = length
    state.item_count = reaper.CountTrackMediaItems(reaper.GetMediaItem_Track(target))
  elseif kind == "item.duplicate" then
    state.position_s = reaper.GetMediaItemInfo_Value(target, "D_POSITION")
    state.length_s = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
    state.item_count = reaper.CountTrackMediaItems(reaper.GetMediaItem_Track(target))
  elseif kind == "item.delete" then
    state.position_s = reaper.GetMediaItemInfo_Value(target, "D_POSITION")
    state.length_s = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
  elseif kind == "marker.create" then
    local _, marker_count = reaper.CountProjectMarkers(project)
    state.marker_count = marker_count
  elseif kind == "take.set" then
    local take = reaper.GetActiveTake(target)
    if not take then
      fail("stale_context", "The item has no active take",
           { target_guid = operation.target_guid })
    end
    state.gain = reaper.GetMediaItemTakeInfo_Value(take, "D_VOL")
    state.pan = reaper.GetMediaItemTakeInfo_Value(take, "D_PAN")
    state.pitch = reaper.GetMediaItemTakeInfo_Value(take, "D_PITCH")
    state.playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE")
  elseif kind == "send.create" then
    state.send_count = reaper.GetTrackNumSends(target, 0)
    state.destination_guid = values.destination_guid
  elseif kind == "send.delete" then
    local send_count = reaper.GetTrackNumSends(target, 0)
    if values.send_index >= send_count then
      fail("stale_context", "The send target no longer exists",
           { track_guid = operation.target_guid, send_index = values.send_index,
             send_count = send_count })
    end
    state.send_count = send_count
    state.send_index = values.send_index
  elseif kind == "envelope.set" then
    local envelope = find_envelope(target, values.envelope)
    if not envelope then
      fail("stale_context", "The envelope does not exist on this track",
           { track_guid = operation.target_guid, envelope = values.envelope })
    end
    state.envelope = values.envelope
    state.point_count = reaper.CountEnvelopePoints(envelope)
    state.scaling_mode = reaper.GetEnvelopeScalingMode(envelope)
  elseif kind == "tempo.set" then
    local numerator, denominator, bpm = reaper.TimeMap_GetTimeSigAtTime(
      project, values.time_s or 0)
    state.bpm = bpm
    state.numerator = numerator
    state.denominator = denominator
    state.time_s = values.time_s or 0
  elseif kind == "region.delete" then
    local _, marker_count = reaper.CountProjectMarkers(project)
    state.marker_count = marker_count
  elseif kind == "track.reorder" then
    local _, index = resolve_track(project, operation.target_guid)
    state.track_index = index
  else
    local fx_index = find_fx_by_guid(target, values.fx_guid)
    if not fx_index then
      fail("stale_context", "The FX target no longer exists",
           { track_guid = operation.target_guid, fx_guid = values.fx_guid })
    end
    state.fx_guid = values.fx_guid
    if kind == "fx.set_enabled" then
      state.enabled = reaper.TrackFX_GetEnabled(target, fx_index)
    elseif kind == "fx.delete" then
      local name_ok, fx_name = reaper.TrackFX_GetFXName(target, fx_index)
      state.fx_index = fx_index
      state.fx_name = name_ok and fx_name or ""
    elseif kind == "fx.set_preset" then
      local preset_ok, preset = reaper.TrackFX_GetPreset(target, fx_index)
      state.preset = preset_ok and preset or ""
    elseif kind == "fx.move" then
      state.fx_index = fx_index
    elseif kind == "fx.offline" then
      state.offline = reaper.TrackFX_GetOffline(target, fx_index)
    elseif kind == "fx.get_chunk" then
      state.fx_index = fx_index
    else
      state.parameter_index = values.parameter_index
      state.normalized = reaper.TrackFX_GetParamNormalized(
        target, fx_index, values.parameter_index)
    end
  end
  return state
end

local function read_edit_state_after(project, operation)
  local kind, values = operation.kind, operation.values
  if kind == "track.delete" then
    return { exists = resolve_track(project, operation.target_guid) ~= nil }
  elseif kind == "item.delete" then
    return { exists = resolve_item(project, operation.target_guid) ~= nil }
  elseif kind == "fx.delete" then
    local track = resolve_track(project, operation.target_guid)
    local fx_index = track and find_fx_by_guid(track, values.fx_guid) or nil
    return { exists = fx_index ~= nil, parent_exists = track ~= nil,
             fx_guid = values.fx_guid, fx_index = fx_index }
  elseif kind == "send.delete" then
    local track = resolve_track(project, operation.target_guid)
    local count = track and reaper.GetTrackNumSends(track, 0) or 0
    return { exists = track ~= nil and values.send_index < count,
             parent_exists = track ~= nil, send_index = values.send_index,
             send_count = count }
  elseif kind == "item.split" then
    local item = resolve_item(project, operation.target_guid)
    local track = item and reaper.GetMediaItem_Track(item) or nil
    return { item_count = track and reaper.CountTrackMediaItems(track) or 0 }
  end
  return read_edit_state(project, operation)
end

local function changed_value(before, after)
  if type(before) == "number" and type(after) == "number" then
    return math.abs(before - after) > 1e-12
  end
  return before ~= after
end

local function inspect_edit(project, operation)
  local normalized = normalize_edit_operation(project, operation)
  local before = read_edit_state(project, normalized)
  local desired = normalized.values
  local changed = false
  if normalized.kind == "track.set" or normalized.kind == "item.set" then
    for key, value in pairs(desired) do
      if key == "stretch_markers" then
        changed = true
      elseif changed_value(before[key], value) then
        changed = true
      end
    end
  elseif normalized.kind == "midi.create" then
    changed = true
  elseif normalized.kind == "fx.insert" or normalized.kind == "fx.delete" or
         normalized.kind == "track.create" or normalized.kind == "track.delete" or
         normalized.kind == "item.create" or normalized.kind == "item.delete" or
         normalized.kind == "marker.create" or normalized.kind == "send.create" or
         normalized.kind == "send.delete" or normalized.kind == "item.split" or
         normalized.kind == "item.duplicate" or normalized.kind == "envelope.set" or
         normalized.kind == "region.delete" or normalized.kind == "track.reorder" then
    changed = true
  elseif normalized.kind == "tempo.set" then
    if desired.beat ~= nil or desired.remove == true then
      changed = true
    else
      changed = changed_value(before.bpm, desired.bpm) or
                changed_value(before.numerator,
                              desired.numerator or before.numerator) or
                changed_value(before.denominator,
                              desired.denominator or before.denominator)
    end
  elseif normalized.kind == "take.set" then
    for key, value in pairs(desired) do
      if changed_value(before[key], value) then changed = true end
    end
  elseif normalized.kind == "send.set" then
    for key, value in pairs(desired) do
      if key ~= "send_index" and changed_value(before[key], value) then
        changed = true
      end
    end
  elseif normalized.kind == "fx.set_enabled" then
    changed = before.enabled ~= desired.enabled
  elseif normalized.kind == "fx.set_preset" then
    changed = before.preset ~= desired.preset
  elseif normalized.kind == "fx.offline" then
    changed = before.offline ~= desired.offline
  elseif normalized.kind == "fx.move" then
    changed = true
  elseif normalized.kind == "fx.get_chunk" then
    changed = true
  else
    changed = changed_value(before.normalized, desired.normalized)
  end
  return { operation = normalized, before = before, changed = changed }
end

local function same_revision(left, right)
  return type(left) == "table" and type(right) == "table" and
         left.project_id == right.project_id and left.path == right.path and
         left.change_count == right.change_count
end

local function transaction_preview(arguments, request_id)
  local revision = arguments and arguments.revision
  if type(revision) ~= "table" or type(revision.project_id) ~= "string" then
    fail("validation_error", "transaction.preview requires a project revision")
  end
  local project, path = find_project(revision.project_id)
  if not project then
    fail("stale_context", "The project in the edit plan is no longer open",
         { project_id = revision.project_id })
  end
  local current = assert_revision(project, path, revision)
  local operations = arguments.operations
  if type(operations) ~= "table" or #operations == 0 or #operations > 256 then
    fail("validation_error", "An edit plan must contain 1 to 256 operations")
  end
  local normalized, before = json.array(), json.array()
  for index = 1, #operations do
    local inspected = inspect_edit(project, operations[index])
    normalized[index] = inspected.operation
    before[index] = { operation_index = index,
                      kind = inspected.operation.kind,
                      target_guid = inspected.operation.target_guid,
                      values = inspected.before,
                      changes_state = inspected.changed }
  end

  local now = os.time()
  for index = #transaction_plan_order, 1, -1 do
    local plan_id = transaction_plan_order[index]
    local stored = transaction_plans[plan_id]
    if not stored or now - stored.created_epoch > transaction_plan_ttl_s then
      transaction_plans[plan_id] = nil
      table.remove(transaction_plan_order, index)
    end
  end
  while #transaction_plan_order >= max_transaction_plans do
    local expired = table.remove(transaction_plan_order, 1)
    transaction_plans[expired] = nil
  end
  local plan_id = bridge_session_id .. ":" .. request_id
  transaction_plans[plan_id] = {
    created_epoch = now, revision = current, operations = normalized, before = before,
  }
  transaction_plan_order[#transaction_plan_order + 1] = plan_id
  return { plan_id = plan_id, revision = current, operations = normalized,
           before = before, expires_in_s = transaction_plan_ttl_s }
end

local function apply_edit(project, inspected)
  local operation = inspected.operation
  if not inspected.changed then return { changed = false } end
  local kind, values = operation.kind, operation.values
  local target
  if kind == "item.set" or kind == "item.delete" or kind == "item.split" or
     kind == "item.duplicate" or kind == "take.set" then
    target = resolve_item(project, operation.target_guid)
  elseif kind == "track.create" or kind == "marker.create" or kind == "tempo.set" or
         kind == "region.delete" then
    target = project
  else
    target = resolve_track(project, operation.target_guid)
  end
  if not target then
    fail("stale_context", "The edit target no longer exists during apply",
         { kind = kind, target_guid = operation.target_guid })
  end
  local function checked_call(fn, ...)
    local ok, result = pcall(fn, ...)
    if not ok or result == false then
      fail("transaction_apply_failed", "A REAPER setter rejected the edit",
           { kind = kind, target_guid = operation.target_guid,
             reason = ok and "setter returned false" or tostring(result) })
    end
  end
  if kind == "midi.create" then
    for _, name in ipairs({ "CreateNewMIDIItemInProj", "MIDI_GetPPQPosFromProjTime",
                            "MIDI_InsertNote", "MIDI_Sort" }) do
      if not reaper.APIExists(name) then
        fail("api_unavailable", "REAPER lacks a required MIDI API function",
             { name = name, version = reaper.GetAppVersion() })
      end
    end
    local item = reaper.CreateNewMIDIItemInProj(
      target, values.position_s, values.position_s + values.length_s, false)
    if not item then
      fail("transaction_apply_failed", "REAPER did not create the MIDI item",
           { target_guid = operation.target_guid })
    end
    local take = reaper.GetActiveTake(item)
    if not take or not reaper.TakeIsMIDI(take) then
      fail("transaction_apply_failed", "REAPER did not create an active MIDI take",
           { target_guid = operation.target_guid })
    end
    for note_index, note in ipairs(values.notes) do
      local start_ppq = reaper.MIDI_GetPPQPosFromProjTime(
        take, values.position_s + note.start_s)
      local end_ppq = reaper.MIDI_GetPPQPosFromProjTime(
        take, values.position_s + note.end_s)
      if not finite_number(start_ppq) or not finite_number(end_ppq) or end_ppq <= start_ppq then
        fail("transaction_apply_failed", "REAPER returned invalid MIDI note positions",
             { note_index = note_index - 1, start_ppq = start_ppq, end_ppq = end_ppq })
      end
      local inserted = reaper.MIDI_InsertNote(
        take, false, note.muted, start_ppq, end_ppq, note.channel, note.pitch,
        note.velocity, true)
      if inserted ~= true then
        fail("transaction_apply_failed", "REAPER rejected a MIDI note",
             { note_index = note_index - 1 })
      end
    end
    reaper.MIDI_Sort(take)
    reaper.UpdateItemInProject(item)
    local guid_ok, item_guid = reaper.GetSetMediaItemInfo_String(item, "GUID", "", false)
    reaper.UpdateArrange()
    return { changed = true, created_item_guid = guid_ok and item_guid or "",
             notes_inserted = #values.notes, position_s = values.position_s,
             length_s = values.length_s }
  elseif kind == "track.set" then
    local fields = { volume = "D_VOL", pan = "D_PAN", mute = "B_MUTE",
                     record_arm = "I_RECARM", record_mode = "I_RECMODE",
                     record_mode_flags = "I_RECMODE_FLAGS",
                     main_send = "B_MAINSEND" }
    for key, value in pairs(values) do
      if key == "name" then
        local ok, result = pcall(reaper.GetSetMediaTrackInfo_String,
                                 target, "P_NAME", value, true)
        if not ok or result == false then
          fail("transaction_apply_failed", "REAPER rejected the track name edit",
               { target_guid = operation.target_guid })
        end
      elseif key == "selected" then
        checked_call(reaper.SetTrackSelected, target, value == true)
      else
        local numeric = type(value) == "boolean" and (value and 1 or 0) or value
        checked_call(reaper.SetMediaTrackInfo_Value, target, fields[key], numeric)
      end
    end
  elseif kind == "item.set" then
    local fields = { position_s = "D_POSITION", length_s = "D_LENGTH", mute = "B_MUTE",
                     beat_attachmode = "C_BEATATTACHMODE", auto_stretch = "C_AUTOSTRETCH" }
    local take = reaper.GetActiveTake(target)
    local stretch = values.stretch_ratio
    if stretch ~= nil then
      if not take then
        fail("validation_error", "item.set stretch_ratio needs an audio take",
             { target_guid = operation.target_guid })
      end
      local old_length = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
      checked_call(reaper.SetMediaItemTakeInfo_Value, take, "D_PLAYRATE",
                   1.0 / stretch)
      checked_call(reaper.SetMediaItemTakeInfo_Value, take, "B_PPITCH",
                   values.preserve_pitch == false and 0 or 1)
      checked_call(reaper.SetMediaItemInfo_Value, target, "D_LENGTH",
                   old_length * stretch)
    elseif values.preserve_pitch ~= nil and take then
      checked_call(reaper.SetMediaItemTakeInfo_Value, take, "B_PPITCH",
                   values.preserve_pitch and 1 or 0)
    end
    if values.stretch_markers ~= nil then
      if not take then
        fail("validation_error", "item.set stretch_markers needs an audio take",
             { target_guid = operation.target_guid })
      end
      for index, marker in ipairs(values.stretch_markers) do
        local marker_index = reaper.SetTakeStretchMarker(
          take, -1, marker.position_s, marker.source_position_s)
        if marker_index < 0 then
          fail("transaction_apply_failed", "REAPER rejected a stretch marker",
               { marker_index = index, position_s = marker.position_s })
        end
      end
    end
    for key, value in pairs(values) do
      if key ~= "stretch_ratio" and key ~= "preserve_pitch" and
         key ~= "stretch_markers" then
        local numeric = type(value) == "boolean" and (value and 1 or 0) or value
        checked_call(reaper.SetMediaItemInfo_Value, target, fields[key], numeric)
      end
    end
    reaper.UpdateItemInProject(target)
  elseif kind == "fx.insert" then
    local index = reaper.TrackFX_AddByName(target, values.name, false,
                                           values.position or -1)
    if not finite_number(index) or index < 0 then
      fail("transaction_apply_failed", "REAPER could not insert the FX",
           { name = values.name })
    end
    return { changed = true, fx_index = index,
             fx_guid = reaper.TrackFX_GetFXGUID(target, index) or "" }
  elseif kind == "fx.delete" then
    local fx_index = find_fx_by_guid(target, values.fx_guid)
    if not fx_index then
      fail("stale_context", "The FX target no longer exists during apply",
           { track_guid = operation.target_guid, fx_guid = values.fx_guid })
    end
    checked_call(reaper.TrackFX_Delete, target, fx_index)
    return { changed = true, deleted_fx_guid = values.fx_guid }
  elseif kind == "send.set" then
    local fields = { volume = "D_VOL", pan = "D_PAN", mute = "B_MUTE" }
    for key, value in pairs(values) do
      if key ~= "send_index" then
        local numeric = type(value) == "boolean" and (value and 1 or 0) or value
        checked_call(reaper.SetTrackSendInfo_Value, target, 0,
                     values.send_index, fields[key], numeric)
      end
    end
  elseif kind == "send.create" then
    local destination = resolve_track(project, values.destination_guid)
    local sends = reaper.CreateTrackSend(target, destination)
    if not finite_number(sends) or sends < 0 then
      fail("transaction_apply_failed", "REAPER did not create the send",
           { destination_guid = values.destination_guid })
    end
    local fields = { volume = "D_VOL", pan = "D_PAN", mute = "B_MUTE" }
    for key, value in pairs(values) do
      if key ~= "destination_guid" then
        local numeric = type(value) == "boolean" and (value and 1 or 0) or value
        checked_call(reaper.SetTrackSendInfo_Value, target, 0, sends,
                     fields[key], numeric)
      end
    end
    return { changed = true, send_index = sends }
  elseif kind == "send.delete" then
    checked_call(reaper.RemoveTrackSend, target, 0, values.send_index)
    return { changed = true, removed_send_index = values.send_index }
  elseif kind == "take.set" then
    local take = reaper.GetActiveTake(target)
    if not take then
      fail("stale_context", "The item has no active take during apply",
           { target_guid = operation.target_guid })
    end
    local fields = { gain = "D_VOL", pan = "D_PAN", pitch = "D_PITCH",
                     playrate = "D_PLAYRATE" }
    for key, value in pairs(values) do
      checked_call(reaper.SetMediaItemTakeInfo_Value, take, fields[key], value)
    end
    reaper.UpdateItemInProject(target)
  elseif kind == "envelope.set" then
    local envelope = find_envelope(target, values.envelope)
    if not envelope then
      fail("stale_context", "The envelope does not exist on this track during apply",
           { track_guid = operation.target_guid, envelope = values.envelope })
    end
    if values.clear then
      reaper.DeleteEnvelopePointRange(envelope, 0,
                                      reaper.GetProjectLength(project) + 1)
    end
    for index, point in ipairs(values.points) do
      local inserted = reaper.InsertEnvelopePoint(
        envelope, point.time_s, point.value, point.shape or 0,
        point.tension or 0, point.selected or false, true)
      if inserted ~= true then
        fail("transaction_apply_failed", "REAPER rejected an envelope point",
             { point_index = index - 1, envelope = values.envelope })
      end
    end
    reaper.Envelope_SortPoints(envelope)
    reaper.UpdateArrange()
    return { changed = true, envelope = values.envelope,
             points_inserted = #values.points, cleared = values.clear == true }
  elseif kind == "tempo.set" then
    local time_s = values.time_s or 0.0
    if values.remove == true then
      local removed = 0
      local count = reaper.CountTempoTimeSigMarkers(project)
      for index = count - 1, 0, -1 do
        local ok, marker_time = reaper.GetTempoTimeSigMarker(project, index)
        if ok and math.abs(marker_time - time_s) < 1e-6 then
          reaper.DeleteTempoTimeSigMarker(project, index)
          removed = removed + 1
        end
      end
      reaper.UpdateTimeline()
      return { changed = true, removed = removed, time_s = time_s }
    end
    if values.beat ~= nil then
      local measure = math.floor(values.beat / 4)
      local beat_in_measure = values.beat - measure * 4
      local marker_index = -1
      local marker_count = reaper.CountTempoTimeSigMarkers(project)
      for index = 0, marker_count - 1 do
        local ok, _, marker_measure, marker_beat =
          reaper.GetTempoTimeSigMarker(project, index)
        if ok and marker_measure == measure and
           math.abs(marker_beat - beat_in_measure) < 1e-6 then
          marker_index = index
          break
        end
      end
      local numerator = values.numerator
      local denominator = values.denominator
      if numerator == nil then
        numerator, denominator = reaper.TimeMap_GetTimeSigAtTime(project, 0)
      end
      local ok = reaper.SetTempoTimeSigMarker(
        project, marker_index, -1, measure, beat_in_measure, values.bpm,
        numerator, denominator, false)
      if not ok then
        fail("transaction_apply_failed", "REAPER rejected the tempo marker at a beat",
             { bpm = values.bpm, beat = values.beat, measure = measure,
               beat_in_measure = beat_in_measure })
      end
      reaper.UpdateTimeline()
      return { changed = true, bpm = values.bpm, beat = values.beat,
               measure = measure, beat_in_measure = beat_in_measure }
    end
    local marker_index = -1
    local marker_count = reaper.CountTempoTimeSigMarkers(project)
    for index = 0, marker_count - 1 do
      local ok, marker_time = reaper.GetTempoTimeSigMarker(project, index)
      if ok and math.abs(marker_time - time_s) < 1e-6 then
        marker_index = index
        break
      end
    end
    if values.numerator then
      local ok = reaper.SetTempoTimeSigMarker(
        project, marker_index, time_s, -1, -1, values.bpm, values.numerator,
        values.denominator, false)
      if not ok then
        fail("transaction_apply_failed", "REAPER rejected the tempo/time-signature marker",
             { bpm = values.bpm, numerator = values.numerator,
               denominator = values.denominator, time_s = time_s })
      end
    elseif values.time_s ~= nil then
      local numerator, denominator = reaper.TimeMap_GetTimeSigAtTime(project, time_s)
      local ok = reaper.SetTempoTimeSigMarker(
        project, marker_index, time_s, -1, -1, values.bpm, numerator, denominator, false)
      if not ok then
        fail("transaction_apply_failed", "REAPER rejected the tempo marker",
             { bpm = values.bpm, time_s = time_s })
      end
    else
      reaper.SetCurrentBPM(project, values.bpm, true)
    end
    reaper.UpdateTimeline()
    return { changed = true, bpm = values.bpm, time_s = time_s }
  elseif kind == "region.delete" then
    local deleted = reaper.DeleteProjectMarker(project, values.marker_index,
                                               values.is_region or false)
    if deleted ~= true then
      fail("transaction_apply_failed", "REAPER did not delete the marker/region",
           { marker_index = values.marker_index,
             is_region = values.is_region or false })
    end
    reaper.UpdateArrange()
    return { changed = true, deleted_marker_index = values.marker_index }
  elseif kind == "track.reorder" then
    local previous = {}
    for index = 0, reaper.CountTracks(project) - 1 do
      local track = reaper.GetTrack(project, index)
      previous[#previous + 1] = { track = track,
                                  selected = reaper.IsTrackSelected(track) }
      reaper.SetTrackSelected(track, false)
    end
    reaper.SetTrackSelected(target, true)
    local moved = reaper.ReorderSelectedTracks(values.before_index, 0)
    for _, entry in ipairs(previous) do
      reaper.SetTrackSelected(entry.track, entry.selected)
    end
    if moved ~= true then
      fail("transaction_apply_failed", "REAPER did not reorder the track",
           { before_index = values.before_index })
    end
    reaper.UpdateArrange()
    return { changed = true, before_index = values.before_index }
  elseif kind == "item.split" then
    local new_item = reaper.SplitMediaItem(target, values.position_s)
    if not new_item then
      fail("transaction_apply_failed", "REAPER did not split the media item",
           { target_guid = operation.target_guid,
             position_s = values.position_s })
    end
    -- tail_s is the total extension of the left slice beyond the original cut.
    -- This matches seam_search's geometry: left_end = cut + tail_s. The right
    -- item can move independently, so the actual overlap is tail - move - trim.
    local fade = values.fade_s or 0.02
    local tail = values.tail_s or 0.0
    local right_trim = values.right_trim_s or 0.0
    local move = values.move_s or 0.0
    local new_start = values.position_s + move
    if new_start + right_trim < 0 then
      fail("validation_error", "item.split move_s would place the item before zero")
    end
    local left_length = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
    if tail > 0 then
      reaper.SetMediaItemInfo_Value(target, "D_LENGTH",
                                    left_length + tail)
    end
    if move ~= 0 or right_trim > 0 then
      reaper.SetMediaItemInfo_Value(new_item, "D_POSITION",
                                    new_start + right_trim)
    end
    local right_length = reaper.GetMediaItemInfo_Value(new_item, "D_LENGTH")
    local left_start = reaper.GetMediaItemInfo_Value(target, "D_POSITION")
    local left_new_length = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
    local right_start = reaper.GetMediaItemInfo_Value(new_item, "D_POSITION")
    local overlap = math.max(0.0, left_start + left_new_length - right_start)
    -- With an overlap, use exactly the overlap measured by the candidate
    -- assembler. Without one, retain the requested edge fades around a gap.
    local fade_length = math.min(overlap > 0 and overlap or fade,
                                 left_new_length, right_length)
    reaper.SetMediaItemInfo_Value(target, "D_FADEOUTLEN", fade_length)
    reaper.SetMediaItemInfo_Value(new_item, "D_FADEINLEN", fade_length)
    reaper.UpdateItemInProject(target)
    reaper.UpdateItemInProject(new_item)
    local guid_ok, new_guid = reaper.GetSetMediaItemInfo_String(
      new_item, "GUID", "", false)
    return { changed = true, new_item_guid = guid_ok and new_guid or "",
             position_s = values.position_s, fade_s = fade_length, tail_s = tail,
             right_trim_s = right_trim, move_s = move }
  elseif kind == "item.duplicate" then
    local source_take = reaper.GetActiveTake(target)
    if not source_take then
      fail("validation_error", "item.duplicate needs an item with an active take",
           { target_guid = operation.target_guid })
    end
    local source = reaper.GetMediaItemTake_Source(source_take)
    local source_path = source and reaper.GetMediaSourceFileName(source) or ""
    if source_path == "" then
      fail("validation_error",
           "item.duplicate supports file-based audio items only",
           { target_guid = operation.target_guid })
    end
    local track = reaper.GetMediaItem_Track(target)
    local position = reaper.GetMediaItemInfo_Value(target, "D_POSITION")
    local length = reaper.GetMediaItemInfo_Value(target, "D_LENGTH")
    local copies = values.copies or 1
    local created = json.array()
    for copy_index = 1, copies do
      local new_item = reaper.AddMediaItemToTrack(track)
      if not new_item then
        fail("transaction_apply_failed", "REAPER did not create a duplicate item",
             { copy_index = copy_index })
      end
      local new_source = reaper.PCM_Source_CreateFromFile(source_path)
      if not new_source then
        fail("transaction_apply_failed", "REAPER could not reopen the item source",
             { source_path = source_path })
      end
      local new_take = reaper.AddTakeToMediaItem(new_item)
      if not new_take then
        fail("transaction_apply_failed", "REAPER did not create a duplicate take")
      end
      reaper.SetMediaItemTake_Source(new_take, new_source)
      for _, key in ipairs({ "D_STARTOFFS", "D_VOL", "D_PAN",
                             "D_PLAYRATE", "D_PITCH" }) do
        reaper.SetMediaItemTakeInfo_Value(
          new_take, key, reaper.GetMediaItemTakeInfo_Value(source_take, key))
      end
      reaper.SetMediaItemInfo_Value(
        new_item, "D_POSITION", position + values.offset_s * copy_index)
      reaper.SetMediaItemInfo_Value(new_item, "D_LENGTH", length)
      reaper.SetMediaItemInfo_Value(
        new_item, "D_FADEINLEN",
        reaper.GetMediaItemInfo_Value(target, "D_FADEINLEN"))
      reaper.SetMediaItemInfo_Value(
        new_item, "D_FADEOUTLEN",
        reaper.GetMediaItemInfo_Value(target, "D_FADEOUTLEN"))
      reaper.UpdateItemInProject(new_item)
      local guid_ok, new_guid = reaper.GetSetMediaItemInfo_String(
        new_item, "GUID", "", false)
      created[#created + 1] = guid_ok and new_guid or ""
    end
    reaper.UpdateArrange()
    return { changed = true, copies = copies,
             created_item_guids = created }
  elseif kind == "track.create" then
    local count = reaper.CountTracks(project)
    local position = values.position or count
    if position > count then position = count end
    reaper.InsertTrackAtIndex(position, true)
    local track = reaper.GetTrack(project, position)
    if not track then
      fail("transaction_apply_failed", "REAPER did not create the track",
           { position = position })
    end
    checked_call(reaper.GetSetMediaTrackInfo_String, track, "P_NAME",
                 values.name, true)
    reaper.UpdateArrange()
    return { changed = true, track_index = position,
             created_track_guid = reaper.GetTrackGUID(track) }
  elseif kind == "track.delete" then
    local deleted_guid = reaper.GetTrackGUID(target)
    reaper.DeleteTrack(target)
    reaper.UpdateArrange()
    return { changed = true, deleted_track_guid = deleted_guid }
  elseif kind == "item.create" then
    local item = reaper.AddMediaItemToTrack(target)
    if not item then
      fail("transaction_apply_failed", "REAPER did not create the media item",
           { target_guid = operation.target_guid })
    end
    checked_call(reaper.SetMediaItemInfo_Value, item, "D_POSITION",
                 values.position_s)
    checked_call(reaper.SetMediaItemInfo_Value, item, "D_LENGTH",
                 values.length_s)
    if values.file then
      local source = reaper.PCM_Source_CreateFromFile(values.file)
      if not source then
        fail("transaction_apply_failed", "REAPER could not open the media file",
             { file = values.file })
      end
      local take = reaper.AddTakeToMediaItem(item)
      if not take then
        fail("transaction_apply_failed", "REAPER did not create a take")
      end
      checked_call(reaper.SetMediaItemTake_Source, take, source)
    end
    reaper.UpdateItemInProject(item)
    local guid_ok, item_guid = reaper.GetSetMediaItemInfo_String(
      item, "GUID", "", false)
    return { changed = true, created_item_guid = guid_ok and item_guid or "",
             position_s = values.position_s, length_s = values.length_s }
  elseif kind == "item.delete" then
    local track = reaper.GetMediaItem_Track(target)
    if not track then
      fail("stale_context", "The item has no track during apply",
           { target_guid = operation.target_guid })
    end
    local _, deleted_guid = reaper.GetSetMediaItemInfo_String(
      target, "GUID", "", false)
    reaper.DeleteTrackMediaItem(track, target)
    reaper.UpdateArrange()
    return { changed = true, deleted_item_guid = deleted_guid or "" }
  elseif kind == "marker.create" then
    local is_region = values.region_length_s ~= nil
    local marker_index = reaper.AddProjectMarker2(
      project, is_region, values.position_s,
      values.position_s + (values.region_length_s or 0), values.name, -1,
      values.color or 0)
    if not finite_number(marker_index) or marker_index < 0 then
      fail("transaction_apply_failed", "REAPER did not create the marker")
    end
    reaper.UpdateArrange()
    return { changed = true, marker_index = marker_index,
             region = is_region }
  else
    local fx_index = find_fx_by_guid(target, values.fx_guid)
    if not fx_index then
      fail("stale_context", "The FX target no longer exists during apply",
           { track_guid = operation.target_guid, fx_guid = values.fx_guid })
    end
    if kind == "fx.get_chunk" then
      local chunk_ok, chunk = reaper.GetTrackStateChunk(target, "", false)
      if not chunk_ok or type(chunk) ~= "string" or chunk == "" then
        fail("transaction_apply_failed", "REAPER did not return a track chunk",
             { fx_guid = values.fx_guid })
      end
      return { changed = true, fx_chunk = chunk }
    elseif kind == "fx.set_enabled" then
      checked_call(reaper.TrackFX_SetEnabled, target, fx_index, values.enabled)
    elseif kind == "fx.set_preset" then
      checked_call(reaper.TrackFX_SetPreset, target, fx_index, values.preset)
    elseif kind == "fx.move" then
      local move_ok, move_error = pcall(reaper.TrackFX_CopyToTrack, target,
                                        fx_index, target, values.to_index, true)
      if not move_ok then
        fail("transaction_apply_failed", "REAPER rejected the FX move",
             { reason = tostring(move_error), to_index = values.to_index })
      end
    elseif kind == "fx.offline" then
      local offline_ok, offline_error = pcall(reaper.TrackFX_SetOffline, target,
                                              fx_index, values.offline)
      if not offline_ok then
        fail("transaction_apply_failed", "REAPER rejected the FX offline change",
             { reason = tostring(offline_error) })
      end
    else
      checked_call(reaper.TrackFX_SetParamNormalized, target, fx_index,
                   values.parameter_index, values.normalized)
    end
  end
  return { changed = true }
end

local function transaction_apply(arguments)
  local plan = arguments and arguments.plan
  if type(plan) ~= "table" or type(plan.plan_id) ~= "string" then
    fail("validation_error", "transaction.apply requires a previewed plan")
  end
  if type(arguments.dry_run) ~= "boolean" then
    fail("validation_error", "transaction.apply requires boolean dry_run")
  end
  local stored = transaction_plans[plan.plan_id]
  if not stored or os.time() - stored.created_epoch > transaction_plan_ttl_s then
    transaction_plans[plan.plan_id] = nil
    fail("stale_context", "The edit plan is missing or expired; preview it again",
         { plan_id = plan.plan_id })
  end
  if not same_revision(plan.revision, stored.revision) then
    fail("validation_error", "The submitted plan revision does not match its preview",
         { plan_id = plan.plan_id })
  end
  local project, path = find_project(stored.revision.project_id)
  if not project then
    fail("stale_context", "The project in the edit plan is no longer open",
         { project_id = stored.revision.project_id })
  end
  local current = assert_revision(project, path, stored.revision)
  if arguments.dry_run then
    return { applied = false, dry_run = true, plan_id = plan.plan_id,
             revision = current, before = stored.before,
             operations = stored.operations }
  end

  local reason = plan.reason
  if type(reason) ~= "string" or reason == "" or #reason > 300 then
    fail("validation_error", "Plan reason must contain 1 to 300 characters")
  end
  for _, name in ipairs({ "Undo_BeginBlock2", "Undo_EndBlock2", "Undo_CanUndo2",
                          "Undo_DoUndo2" }) do
    if not reaper.APIExists(name) then
      fail("api_unavailable", "REAPER lacks a required transaction/undo API",
           { name = name, version = reaper.GetAppVersion() })
    end
  end
  local inspected = json.array()
  for index, operation in ipairs(stored.operations) do
    inspected[index] = inspect_edit(project, operation)
  end
  local any_changes = false
  for _, entry in ipairs(inspected) do
    if entry.changed then any_changes = true break end
  end
  if not any_changes then
    transaction_plans[plan.plan_id] = nil
    return { applied = true, dry_run = false, no_op = true,
             plan_id = plan.plan_id, revision_before = current,
             revision_after = current, before = stored.before,
             after = stored.before, undo_available = false }
  end

  local undo_label = "REAPER Workbench " .. plan.plan_id:sub(-12)
  reaper.Undo_BeginBlock2(project)
  reaper.PreventUIRefresh(1)
  local ok, outcome = xpcall(function()
    local results = json.array()
    for index, entry in ipairs(inspected) do
      results[index] = apply_edit(project, entry) or { changed = true }
    end
    local after = json.array()
    for index, operation in ipairs(stored.operations) do
      local state = read_edit_state_after(project, operation)
      if (operation.kind == "track.delete" or operation.kind == "item.delete" or
          operation.kind == "fx.delete" or operation.kind == "send.delete") and
          state.exists then
        fail("transaction_verification_failed",
             "REAPER still reports the object after a delete operation",
             { kind = operation.kind, target_guid = operation.target_guid,
               fx_guid = operation.values.fx_guid })
      end
      after[index] = { operation_index = index, kind = operation.kind,
                       target_guid = operation.target_guid, values = state }
    end
    return { operation_results = results, after = after }
  end, function(err) return err end)
  local failure
  if not ok then failure = outcome end
  local operation_results = ok and outcome.operation_results or json.array()
  reaper.PreventUIRefresh(-1)
  reaper.UpdateArrange()
  reaper.Undo_EndBlock2(project, undo_label, -1)
  if not ok then
    local last_undo = reaper.Undo_CanUndo2(project)
    if last_undo == undo_label then
      local undo_ok, undo_result = pcall(reaper.Undo_DoUndo2, project)
      if not undo_ok or not finite_number(undo_result) or undo_result == 0 then
        fail("transaction_rollback_failed", "The failed edit could not be rolled back",
             { plan_id = plan.plan_id, reason = tostring(failure) })
      end
    else
      local after_failure = project_identity(project, path)
      if after_failure.change_count ~= current.change_count then
        fail("transaction_rollback_failed", "The edit failed and no matching undo record was available",
             { plan_id = plan.plan_id, reason = tostring(failure),
               undo_description = last_undo, revision = after_failure })
      end
    end
    if type(failure) == "table" and failure.bridge_error then
      fail(failure.code, failure.message, failure.details)
    end
    fail("transaction_apply_failed", tostring(failure), { plan_id = plan.plan_id })
  end

  local after = outcome.after
  local after_revision = project_identity(project, path)
  transaction_plans[plan.plan_id] = nil
  -- Some REAPER APIs (notably ReorderSelectedTracks) create their own undo
  -- label inside the block. Store the label that is actually on top so undo
  -- can find this transaction again.
  local actual_undo = reaper.Undo_CanUndo2(project)
  if type(actual_undo) ~= "string" or actual_undo == "" then
    actual_undo = undo_label
  end
  last_transaction_by_project[after_revision.project_id] = {
    undo_label = actual_undo, revision = after_revision,
    plan_id = plan.plan_id,
  }
  return { applied = true, dry_run = false, no_op = false,
           plan_id = plan.plan_id, revision_before = current,
           revision_after = after_revision, before = stored.before,
           after = after, operation_results = operation_results,
           undo_available = true, undo_label = actual_undo }
end

local function transaction_undo(arguments)
  local expected = arguments and arguments.expected_revision
  if type(expected) ~= "table" or type(expected.project_id) ~= "string" then
    fail("validation_error", "transaction.undo requires an expected project revision")
  end
  local project, path = find_project(expected.project_id)
  if not project then
    fail("stale_context", "The project for this undo is no longer open",
         { project_id = expected.project_id })
  end
  local current = project_identity(project, path)
  if current.project_id ~= expected.project_id or current.path ~= expected.path then
    fail("stale_context", "The project identity no longer matches this undo",
         { expected = expected, current = current })
  end
  local last = last_transaction_by_project[current.project_id]
  if not last then
    fail("stale_context", "No library transaction is recorded for this project",
         { current = current })
  end
  local description = reaper.Undo_CanUndo2(project)
  if description ~= last.undo_label then
    fail("stale_context", "A different undoable action is newer than the library transaction",
         { expected_undo = last.undo_label, available_undo = description })
  end
  -- Analysis changes render settings and the change count, but leaves the
  -- transaction as the top undo record. A real user edit changes the undo
  -- description and is rejected by the check above.
  local ok, result = pcall(reaper.Undo_DoUndo2, project)
  if not ok or not finite_number(result) or result == 0 then
    fail("transaction_undo_failed", "REAPER could not undo the library transaction",
         { plan_id = last.plan_id, reason = tostring(result) })
  end
  last_transaction_by_project[current.project_id] = nil
  local after = project_identity(project, path)
  return { undone = true, plan_id = last.plan_id,
           revision_before = current, revision_after = after }
end

local function fx_list_params(arguments)
  local project, path = target_project(arguments)
  local track_guid = arguments.track_guid
  local fx_guid = arguments.fx_guid
  local track = resolve_track(project, track_guid)
  if not track then
    fail("stale_context", "The FX parameter track no longer exists",
         { track_guid = track_guid })
  end
  local fx_index = find_fx_by_guid(track, fx_guid)
  if not fx_index then
    fail("stale_context", "The FX target no longer exists", { fx_guid = fx_guid })
  end
  local parameters = json.array()
  local count = reaper.TrackFX_GetNumParams(track, fx_index)
  for index = 0, count - 1 do
    local name_ok, name = reaper.TrackFX_GetParamName(track, fx_index, index)
    local value, minimum, maximum = reaper.TrackFX_GetParam(track, fx_index, index)
    local formatted_ok, formatted = reaper.TrackFX_GetFormattedParamValue(
      track, fx_index, index)
    local preset_ok, preset = reaper.TrackFX_GetPreset(track, fx_index)
    parameters[#parameters + 1] = {
      index = index,
      name = name_ok and name or "",
      value = value, min = minimum, max = maximum,
      normalized = reaper.TrackFX_GetParamNormalized(track, fx_index, index),
      formatted = formatted_ok and formatted or "",
    }
  end
  return { track_guid = track_guid, fx_guid = fx_guid, fx_index = fx_index,
           count = count, parameters = parameters }
end

local function fx_list_installed(arguments)
  local plugins = json.array()
  local index = 0
  while true do
    local ok, name, ident = reaper.EnumInstalledFX(index)
    if not ok then break end
    plugins[#plugins + 1] = { name = name, ident = ident }
    index = index + 1
    if index > 100000 then break end
  end
  return { count = #plugins, plugins = plugins }
end

local function write_accessor_wav(path, sample_rate, channels, frames,
                                  accessor, start_s, gain)
  local file, open_error = io.open(path, "wb")
  if not file then error("cannot create WAV: " .. tostring(open_error)) end
  local data_bytes = frames * channels * 4
  local function u32(value)
    value = value % 4294967296
    return string.char(value % 256, math.floor(value / 256) % 256,
                       math.floor(value / 65536) % 256,
                       math.floor(value / 16777216) % 256)
  end
  local function u16(value)
    return string.char(value % 256, math.floor(value / 256) % 256)
  end
  file:write("RIFF", u32(36 + data_bytes), "WAVE", "fmt ", u32(16), u16(3),
             u16(channels), u32(sample_rate),
             u32(sample_rate * channels * 4), u16(channels * 4), u16(32),
             "data", u32(data_bytes))
  local chunk_frames = 48000
  local buffer = reaper.new_array(chunk_frames * channels)
  local position = 0
  while position < frames do
    local count = math.min(chunk_frames, frames - position)
    local received = reaper.GetAudioAccessorSamples(
      accessor, sample_rate, channels, start_s + position / sample_rate,
      count, buffer)
    if received == 0 then
      file:close()
      error("REAPER returned no audio accessor samples at frame " .. position)
    end
    local packed = {}
    for index = 1, count * channels do
      packed[index] = string.pack("<f", buffer[index] * gain)
    end
    file:write(table.concat(packed))
    position = position + count
  end
  file:close()
  return data_bytes + 44
end

local function project_sample_tracks(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity(project, path)
  if reaper.GetPlayStateEx(project) ~= 0 then
    fail("validation_error", "Stop playback before sampling audio",
         { play_state = reaper.GetPlayStateEx(project) })
  end
  local output_dir = arguments.output_dir
  local job_id = arguments.job_id
  if type(output_dir) ~= "string" or output_dir == "" or
     not (output_dir:match("^[A-Za-z]:[/\\]") or output_dir:match("^[/\\][/\\]")) then
    fail("validation_error", "output_dir must be an absolute Windows path")
  end
  if type(job_id) ~= "string" or not job_id:match("^%w+$") then
    fail("validation_error", "job_id must be alphanumeric")
  end
  local start_s = arguments.start_s or 0
  local end_s = arguments.end_s
  local sample_rate = arguments.sample_rate or 48000
  if not finite_number(start_s) or not finite_number(end_s) or end_s <= start_s then
    fail("validation_error", "start_s and end_s must define a positive range")
  end
  if not finite_number(sample_rate) or sample_rate < 8000 or sample_rate > 192000 then
    fail("validation_error", "sample_rate is outside the supported range")
  end
  local frame_count = math.floor((end_s - start_s) * sample_rate + 0.5)
  if frame_count <= 0 or frame_count > 48000 * 60 * 60 then
    fail("validation_error", "requested sample range is invalid")
  end
  local files = json.array()
  local track_count = reaper.CountTracks(project)
  for index = 0, track_count - 1 do
    local track = reaper.GetTrack(project, index)
    local accessor = reaper.CreateTrackAudioAccessor(track)
    if not accessor then
      fail("api_unavailable", "Could not create an audio accessor for track " .. index)
    end
    local name_ok, name = reaper.GetSetMediaTrackInfo_String(
      track, "P_NAME", "", false)
    if not name_ok then name = "Track " .. (index + 1) end
    local safe_name = name:gsub("[^%w%-_ ]", "_")
    if #safe_name > 40 then safe_name = safe_name:sub(1, 40) end
    local file_path = output_dir .. "/rali-" .. job_id .. "-" ..
                      string.format("%02d", index + 1) .. "-" .. safe_name .. ".wav"
    local gain = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    if not finite_number(gain) then gain = 1 end
    local bytes = write_accessor_wav(file_path, sample_rate, 2, frame_count,
                                     accessor, start_s, gain)
    reaper.DestroyAudioAccessor(accessor)
    files[#files + 1] = { source_guid = reaper.GetTrackGUID(track),
                          name = name, track_index = index, is_master = false,
                          path = file_path, bytes = bytes }
  end
  return { project_id = revision.project_id, revision = revision,
           files = files, capture_point = "native_audio_accessor_tracks",
           sample_rate = sample_rate, frame_count = frame_count }
end

local operations = {
  health = function()
    local project, path = active_project()
    return { service = "reaper-workbench", protocol = protocol_version,
             bridge_session = bridge_session_id,
             reaper_version = reaper.GetAppVersion(), project = project_identity(project, path),
             open_projects = projects_list() }
  end,
  ["projects.list"] = projects_list,
  ["project.context"] = project_context,
  ["project.tempo_at"] = project_tempo_at,
  ["project.render_settings.read"] = project_render_settings_read,
  ["project.capture_render"] = project_capture_render,
  ["project.sample_tracks"] = project_sample_tracks,
  ["fx.list_installed"] = fx_list_installed,
  ["fx.list_params"] = fx_list_params,
  ["project.save"] = project_save,
  ["transaction.preview"] = transaction_preview,
  ["transaction.apply"] = transaction_apply,
  ["transaction.undo"] = transaction_undo,
  ["actions.list"] = action_list,
  ["action.invoke"] = action_invoke,
  ["api.exists"] = function(arguments)
    local name = arguments.name
    if type(name) ~= "string" then error("name must be a string") end
    return { name = name, exists = reaper.APIExists(name),
             reaper_version = reaper.GetAppVersion() }
  end,
  ["api.call"] = api_call,
}

local function write_response(request_id, response)
  local final_path = response_dir .. "/" .. request_id .. ".json"
  local temp_path = final_path .. ".tmp"
  local file, open_error = io.open(temp_path, "wb")
  if not file then error("cannot write bridge response: " .. tostring(open_error)) end
  local encode_ok, encoded = pcall(json.encode, response)
  if not encode_ok then
    encoded = json.encode({ id = request_id, ok = false, error = "encode_failed",
                            message = tostring(encoded) })
  end
  file:write(encoded)
  file:close()
  os.remove(final_path)
  local ok, rename_error = os.rename(temp_path, final_path)
  if not ok then error("cannot publish bridge response: " .. tostring(rename_error)) end
end

local function read_request()
  local file = io.open(request_path, "rb")
  if not file then return nil end
  local size = file:seek("end")
  if size and size > max_request_bytes then
    file:close()
    os.remove(request_path)
    return { malformed = true, error = "request exceeds size limit" }
  end
  file:seek("set", 0)
  local contents = file:read("*a")
  file:close()
  os.remove(request_path)
  local ok, value = pcall(json.decode, contents or "")
  if not ok or type(value) ~= "table" then
    return { malformed = true, error = tostring(value) }
  end
  return value
end

local function process_request(request)
  local request_id = request.id
  if type(request_id) ~= "string" or not request_id:match("^%x+$") then return end
  if request.protocol ~= protocol_version then
    write_response(request_id, { id = request_id, ok = false, error = "validation_error",
      message = "Unsupported bridge protocol", details = { received = request.protocol,
                                                               supported = protocol_version } })
    return
  end
  if not finite_number(request.deadline_epoch) or request.deadline_epoch < os.time() then
    write_response(request_id, { id = request_id, ok = false, error = "bridge_timeout",
                                 message = "Request expired before REAPER consumed it" })
    return
  end
  local handler = operations[request.operation]
  if not handler then
    write_response(request_id, { id = request_id, ok = false, error = "validation_error",
                                 message = "Unsupported operation",
                                 details = { operation = request.operation } })
    return
  end
  local arguments = request.arguments or {}
  local ok, result = xpcall(function() return handler(arguments, request_id) end,
                            function(err) return err end)
  if not ok then
    if type(result) == "table" and result.bridge_error then
      write_response(request_id, { id = request_id, ok = false,
        error = result.code, message = result.message, details = result.details })
    else
      write_response(request_id, { id = request_id, ok = false, error = "reaper_audio_error",
                                   message = tostring(result), details = {} })
    end
    return
  end
  write_response(request_id, { id = request_id, ok = true, result = result })
end

local last_registry_update = nil
local function loop()
  local now = reaper.time_precise()
  if not last_registry_update or now - last_registry_update >= 2.0 then
    local ok, registry_error = pcall(write_heartbeat)
    if not ok then
      reaper.ShowConsoleMsg("REAPER Workbench discovery error: " ..
                            tostring(registry_error) .. "\n")
    end
    last_registry_update = now
  end
  local request = read_request()
  if request then
    if request.malformed then
      -- A malformed mailbox has no trusted request ID to answer; discard it.
    else
      local ok, error_message = xpcall(function() process_request(request) end, debug.traceback)
      if not ok then
        reaper.ShowConsoleMsg("REAPER Workbench bridge error: " ..
                              tostring(error_message) .. "\n")
      end
    end
  end
  reaper.defer(loop)
end

reaper.atexit(function()
  reaper.set_action_options(8)
  os.remove(registry_path)
  os.remove(registry_path .. ".tmp")
  os.remove(pulse_path)
end)
write_instance_registry()
write_heartbeat()
reaper.defer(loop)
