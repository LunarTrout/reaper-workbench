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
-- Bridge contract build. Bumped whenever the resident script's request surface
-- or its guarantees change, so a client can prove which copy is actually
-- running before it trusts a reload, a capture or an edit.
local bridge_build = "b5-2026-11-14"
-- A second boot inside the same REAPER Lua state must supersede the first:
-- two loops would both keep heartbeating and the client would see two live
-- bridges. Every loop compares this token before it reschedules; the newest
-- boot owns it, so a reload converges to exactly one resident loop.
local supersede_token = string.format("%s:%d", bridge_session_id,
                                      math.floor(reaper.time_precise() * 1000))
_G.reaper_bridge_supersede = supersede_token
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

-- Canonical read-model numbers must stay JSON-encodable. REAPER returns nil or
-- a non-finite value for absent hardware/source fields; report 0 instead of
-- letting the encoder suppress the whole response.
local function finite_or_zero(value)
  if finite_number(value) then return value end
  return 0
end

-- REAPER mixes booleans and 0/1 integers depending on the attribute.
local function flag_true(value)
  return value == true or value == 1
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

-- Continuity identity. The path and change counter cannot prove that the
-- project state is continuous: REAPER restarts reuse both, and a project
-- reloaded from disk starts a new counter generation. The bridge therefore
-- keeps one generation counter per project object for the life of this
-- instance and bumps it whenever continuity is contradicted: a project object
-- never seen before, a different project identity (save-as, replaced master)
-- or a change counter that moved backwards (reload / restore).
--
-- The epoch starts at 1; 0 means "unknown" and can never prove continuity.
-- It is scoped to bridge_session_id, so a REAPER restart changes instance_id
-- and the pair fails closed even though the file path is unchanged.
local project_generations = {}
local project_generation_sequence = 0

local function project_identity_state(project, path)
  local identity = project_identity(project, path)
  local state = project_generations[project]
  if state == nil or state.project_id ~= identity.project_id or
     identity.change_count < state.change_count then
    project_generation_sequence = project_generation_sequence + 1
    state = { epoch = project_generation_sequence,
              project_id = identity.project_id,
              change_count = identity.change_count }
    project_generations[project] = state
  else
    state.change_count = identity.change_count
  end
  identity.instance_id = bridge_session_id
  identity.epoch = state.epoch
  identity.epoch_basis = "bridge_session_project_generation"
  return identity
end

local function same_revision_fields(left, right)
  if type(left) ~= "table" or type(right) ~= "table" then return false end
  if left.project_id ~= right.project_id or left.path ~= right.path or
     left.change_count ~= right.change_count then
    return false
  end
  -- Identity components are compared only when both sides carry them: a
  -- legacy client revision cannot prove them, and the Python gate refuses to
  -- mint a token for such a request. Inside the bridge a missing component
  -- must not silently widen the match either, so a supplied component is
  -- always compared strictly.
  if left.instance_id ~= nil and left.instance_id ~= right.instance_id then
    return false
  end
  if left.epoch ~= nil and left.epoch ~= right.epoch then
    return false
  end
  return true
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

local function normalize_project_id(value)
  if type(value) ~= "string" then return value end
  local lowered = string.lower(value)
  return (string.gsub(lowered, "\\", "/"))
end

local function find_project(project_id)
  local index = 0
  while true do
    local project, path = reaper.EnumProjects(index, "")
    if not project then return nil end
    if normalize_project_id(project_identity(project, path or "").project_id)
        == normalize_project_id(project_id) then
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
    start_offset_s = 0,
    playrate = 0,
    fade_in_s = item and reaper.GetMediaItemInfo_Value(item, "D_FADEINLEN") or 0,
    fade_out_s = item and reaper.GetMediaItemInfo_Value(item, "D_FADEOUTLEN") or 0,
    mute = item and reaper.GetMediaItemInfo_Value(item, "B_MUTE") ~= 0 or false,
    selected = item and reaper.IsMediaItemSelected(item) or false,
    take_count = item and reaper.CountTakes(item) or 0,
    loop_source = false,
    channel_mode = 0,
    group_id = 0,
    beat_attach_mode = 0,
    source_length_s = 0,
    source_length_is_qn = false,
    source_sample_rate = 0,
    source_channels = 0,
    source_online = false,
    stretch_marker_positions = json.array(),
    active_take = json.null,
  }
  if take then
    local take_guid_ok, take_guid = reaper.GetSetMediaItemTakeInfo_String(
      take, "GUID", "", false)
    local source = reaper.GetMediaItemTake_Source(take)
    local source_file = source and reaper.GetMediaSourceFileName(source, "") or ""
    entry.start_offset_s = finite_or_zero(
      reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS"))
    entry.playrate = finite_or_zero(
      reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE"))
    entry.loop_source = flag_true(
      reaper.GetMediaItemTakeInfo_Value(take, "B_LOOPSRC"))
    entry.channel_mode = finite_or_zero(
      reaper.GetMediaItemTakeInfo_Value(take, "I_CHANMODE"))
    entry.group_id = item and finite_or_zero(
      reaper.GetMediaItemInfo_Value(item, "I_GROUPID")) or 0
    entry.beat_attach_mode = finite_or_zero(
      reaper.GetMediaItemTakeInfo_Value(take, "C_BEATATTACHMODE"))
    if source then
      -- MediaSourceLength is quarter notes for beat-based sources; the flag
      -- travels with the number so the unit is never guessed downstream.
      local source_length, length_is_qn = reaper.GetMediaSourceLength(source)
      entry.source_length_s = finite_or_zero(source_length)
      entry.source_length_is_qn = flag_true(length_is_qn)
      entry.source_sample_rate = finite_or_zero(
        reaper.GetMediaSourceSampleRate(source))
      entry.source_channels = finite_or_zero(
        reaper.GetMediaSourceNumChannels(source))
      entry.source_online = flag_true(
        finite_number(source_length) and source_length > 0 and
        entry.source_sample_rate > 0 and entry.source_channels > 0)
    end
    local marker_count = reaper.GetTakeNumStretchMarkers(take)
    for marker_index = 0, (finite_number(marker_count) and marker_count or 0) - 1 do
      local marker_ok, marker_pos, marker_srcpos =
        reaper.GetTakeStretchMarker(take, marker_index)
      if marker_ok and marker_ok >= 0 then
        entry.stretch_marker_positions[#entry.stretch_marker_positions + 1] = {
          pos = finite_or_zero(marker_pos),
          srcpos = finite_or_zero(marker_srcpos),
        }
      end
    end
    entry.active_take = {
      guid = take_guid_ok and take_guid or "",
      name = reaper.GetTakeName(take) or "",
      source = source_file or "",
      is_midi = reaper.TakeIsMIDI(take),
      playrate = reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE"),
      preserve_pitch = reaper.GetMediaItemTakeInfo_Value(take, "B_PPITCH") ~= 0,
      stretch_markers = reaper.GetTakeNumStretchMarkers(take),
      fx_count = reaper.TakeFX_GetCount(take),
      volume = finite_or_zero(reaper.GetMediaItemTakeInfo_Value(take, "D_VOL")),
      pan = finite_or_zero(reaper.GetMediaItemTakeInfo_Value(take, "D_PAN")),
      pitch = finite_or_zero(reaper.GetMediaItemTakeInfo_Value(take, "D_PITCH")),
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
    pan_mode = reaper.GetMediaTrackInfo_Value(track, "I_PANMODE"),
    pan_law = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_PANLAW")),
    pan_law_flags = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_PANLAW_FLAGS")),
    width = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_WIDTH")),
    dualpan_left = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_DUALPANL")),
    dualpan_right = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_DUALPANR")),
    phase_inverted = flag_true(
      reaper.GetMediaTrackInfo_Value(track, "B_PHASE")),
    main_send = flag_true(
      reaper.GetMediaTrackInfo_Value(track, "B_MAINSEND")),
    play_offset = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_PLAY_OFFSET")),
    play_offset_flags = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_PLAY_OFFSET_FLAG")),
    freeze_count = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_FREEZECOUNT")),
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
  local revision = project_identity_state(project, path)
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
  local markers, regions = json.array(), json.array()
  local marker_total, marker_count, region_count =
    reaper.CountProjectMarkers(project)
  if finite_number(marker_total) and marker_total >= 0 then
    for marker_index = 0, marker_total - 1 do
      local ok, is_region, position_s, end_s, name, number, color =
        reaper.EnumProjectMarkers3(project, marker_index)
      if ok then
        local row = { index = marker_index, is_region = flag_true(is_region),
                      position_s = finite_or_zero(position_s),
                      end_s = finite_or_zero(end_s), name = name or "",
                      number = finite_or_zero(number),
                      color = finite_or_zero(color) }
        if is_region then regions[#regions + 1] = row else markers[#markers + 1] = row end
      end
    end
  end
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
    transport = (function()
      local play_state = reaper.GetPlayStateEx(project)
      local edit_cursor_s = reaper.GetCursorPositionEx(project)
      local play_position_s = reaper.GetPlayPositionEx(project)
      return { play_state = play_state,
               cursor_s = edit_cursor_s,
               edit_cursor_s = edit_cursor_s,
               play_position_s = play_position_s,
               play_position_kind = "observed_playback_not_project_cursor" }
    end)(),
    tempo = { bpm = bpm, numerator = numerator, denominator = denominator,
              markers = tempo_map },
    markers = markers, regions = regions,
    marker_count = marker_count, region_count = region_count,
  }
end

-- Canonical content digest (digest_version 1, additive schema).
--
-- Fingerprint invalidation: this schema is a strict superset of the Wave B
-- digest. Master, automation, render/playback settings and FX-chain state are
-- new fields, so every fingerprint recorded before this change is stale by
-- construction -- the earlier digest never measured them.
--
-- Every value below comes from a documented ReaScript API. A subgroup that
-- cannot be read is reported through ``coverage``/``missing``; it is never
-- replaced by an empty value, so a consumer cannot mistake an unmeasured
-- corner of the project for a measured one.
local digest_version = 1

-- Bounds. Exceeding one degrades coverage instead of silently truncating.
local digest_max_envelope_points = 50000
local digest_max_automation_items = 5000
local digest_max_fx_envelope_probes = 2000000
local digest_max_fx_chain_chars = 8388608

-- Envelope chunks repeat two UI-only fields (lane visibility and lane height)
-- that move when the arrange view is resized. Dropping them keeps a window
-- resize from looking like a content edit; ACT/ARM and every point stay in.
local digest_envelope_ui_lines = { VIS = true, LANEHEIGHT = true }

-- FX chain chunks carry window geometry and the last selected slot. Same
-- reasoning: they are UI, not sound. Plugin state blobs, BYPASS flags, FXID,
-- ordering and containers are kept verbatim. UI lines inside nested container
-- nodes are kept as well, which is conservative -- extra churn, never a missed
-- change.
local digest_fx_chain_ui_lines = { WNDRECT = true, SHOW = true, LASTSEL = true,
                                   DOCKED = true, FLOATPOS = true, WAK = true,
                                   MULTIFXLINK = true }

-- Item chunks carry selection, colour and lane-view geometry that move without
-- changing the signal. Everything else (POSITION, LENGTH, fades, take state,
-- TAKEFX, source) stays byte-identical so a selection change cannot invalidate
-- a render key.
local digest_item_ui_lines = { B_UISEL = true, I_CUSTOMCOLOR = true,
                               I_LASTY = true, I_LASTH = true }

-- Render and playback settings that change what the project produces. Output
-- file names, patterns and formats are deliberately excluded: they select a
-- destination rather than project content, and the capture path rewrites them
-- temporarily. Keys are lower-cased in the payload.
local digest_project_settings = {
  "RENDER_SETTINGS", "RENDER_CHANNELS", "RENDER_SRATE", "RENDER_BOUNDSFLAG",
  "RENDER_STARTPOS", "RENDER_ENDPOS", "RENDER_TAILFLAG", "RENDER_TAILMS",
  "RENDER_ADDTOPROJ", "RENDER_DITHER", "RENDER_NORMALIZE",
  "RENDER_NORMALIZE_TARGET", "RENDER_BRICKWALL", "RENDER_FADEIN",
  "RENDER_FADEOUT", "RENDER_FADEINSHAPE", "RENDER_FADEOUTSHAPE",
  "RENDER_FADELPF", "RENDER_PADSTART", "RENDER_PADEND", "RENDER_TRIMSTART",
  "RENDER_TRIMEND", "RENDER_DELAY", "PROJECT_SRATE", "PROJECT_SRATE_USE",
  "PROJECT_TIMEBASE", "PROJECT_TIMEBASE_FLAGS",
}

-- Copy one RPPXML block (for example <FXCHAIN ...> ... >) out of a state
-- chunk. The scan is byte-wise and keeps nothing but the requested block:
-- a track state chunk can carry every item of the track, and only the FX chain
-- is evidence here. Returns nil plus a reason when the block is absent or
-- unbalanced -- a truncated copy would be worse evidence than an admitted gap.
local function chunk_block(chunk, name)
  if type(chunk) ~= "string" or chunk == "" then return nil, "empty_chunk" end
  local opening
  local position = 1
  while position <= #chunk do
    local newline = chunk:find("\n", position, true)
    local line_end = newline and (newline - 1) or #chunk
    local text = chunk:sub(position, line_end)
    local candidate = text:match("^%s*(<.*)$")
    if candidate and candidate:sub(1, #name + 1) == "<" .. name then
      local following = candidate:sub(#name + 2, #name + 2)
      if following == "" or following == ">" or following:match("%s") then
        opening = position
        break
      end
    end
    if not newline then break end
    position = newline + 1
  end
  if not opening then return nil, "absent" end
  local depth = 0
  position = opening
  while position <= #chunk do
    local newline = chunk:find("\n", position, true)
    local line_end = newline and (newline - 1) or #chunk
    local text = chunk:sub(position, line_end)
    if text:match("^%s*<") then
      depth = depth + 1
    elseif text:match("^%s*>%s*$") then
      depth = depth - 1
      if depth == 0 then
        return chunk:sub(opening, line_end)
      end
      if depth < 0 then return nil, "unbalanced" end
    end
    if not newline then break end
    position = newline + 1
  end
  return nil, "unbalanced"
end

-- Drop direct children of the block root whose keyword is UI-only state.
-- Depth 1 is the root's own content; plugin state blobs live deeper and are
-- never touched.
local function normalize_chunk(block, ui_lines)
  local out = {}
  local depth = 0
  for line in (block .. "\n"):gmatch("([^\n]*)\n") do
    local trimmed = line:match("^%s*(.-)%s*$")
    local keyword = trimmed:match("^([A-Z_]+)%s")
    if not (depth == 1 and keyword ~= nil and ui_lines[keyword] == true) then
      out[#out + 1] = line
    end
    if trimmed:sub(1, 1) == "<" then
      depth = depth + 1
    elseif trimmed == ">" then
      depth = depth - 1
    end
  end
  return table.concat(out, "\n")
end

local function envelope_text(env, name)
  if type(reaper.GetSetEnvelopeInfo_String) ~= "function" then return nil end
  local ok, value = reaper.GetSetEnvelopeInfo_String(env, name, "", false)
  if not ok or type(value) ~= "string" then return nil end
  return value
end

local function envelope_flag(env, name)
  local value = envelope_text(env, name)
  if value == "1" then return true end
  if value == "0" then return false end
  return nil
end

-- Read one envelope completely. Returns nil plus a reason when any documented
-- attribute, point or automation item cannot be read, so the caller reports a
-- gap instead of a half-empty envelope.
local function envelope_context(env)
  if not env then return nil, "missing_envelope" end
  if type(reaper.GetEnvelopeStateChunk) ~= "function" or
     type(reaper.GetEnvelopeName) ~= "function" or
     type(reaper.CountEnvelopePoints) ~= "function" or
     type(reaper.GetEnvelopePoint) ~= "function" then
    return nil, "envelope_api_unavailable"
  end
  local chunk_ok, chunk = reaper.GetEnvelopeStateChunk(env, "", false)
  if not chunk_ok or type(chunk) ~= "string" then return nil, "chunk_unreadable" end
  local name_ok, name = reaper.GetEnvelopeName(env, "")
  if not name_ok or type(name) ~= "string" then return nil, "name_unreadable" end
  local guid = envelope_text(env, "GUID")
  local active = envelope_flag(env, "ACTIVE")
  local visible = envelope_flag(env, "VISIBLE")
  local armed = envelope_flag(env, "ARM")
  if guid == nil or guid == "" or active == nil or visible == nil or armed == nil then
    return nil, "attribute_unreadable"
  end
  local point_count = reaper.CountEnvelopePoints(env)
  if not finite_number(point_count) or point_count < 0 then
    return nil, "point_count_unreadable"
  end
  if point_count > digest_max_envelope_points then return nil, "point_limit" end
  local points = json.array()
  for index = 0, point_count - 1 do
    local ok, time, value, shape, tension = reaper.GetEnvelopePoint(env, index)
    if not ok then return nil, "point_unreadable" end
    points[#points + 1] = { index = index, time_s = finite_or_zero(time),
                            value = finite_or_zero(value),
                            shape = finite_or_zero(shape),
                            tension = finite_or_zero(tension) }
  end
  if type(reaper.CountAutomationItems) ~= "function" or
     type(reaper.GetSetAutomationItemInfo) ~= "function" then
    return nil, "automation_item_api_unavailable"
  end
  local item_count = reaper.CountAutomationItems(env)
  if not finite_number(item_count) or item_count < 0 then
    return nil, "automation_item_count_unreadable"
  end
  if item_count > digest_max_automation_items then
    return nil, "automation_item_limit"
  end
  local items = json.array()
  for index = 0, item_count - 1 do
    local function field(desc)
      return finite_or_zero(reaper.GetSetAutomationItemInfo(
        env, index, desc, 0, false))
    end
    items[#items + 1] = {
      index = index,
      position_s = field("D_POSITION"),
      length_s = field("D_LENGTH"),
      start_offset_s = field("D_STARTOFFS"),
      playrate = field("D_PLAYRATE"),
      baseline = field("D_BASELINE"),
      amplitude = field("D_AMPLITUDE"),
      loop_source = flag_true(field("D_LOOPSRC")),
      mute = flag_true(field("D_MUTE")),
      pool_id = field("D_POOL_ID"),
    }
  end
  return {
    guid = guid,
    name = name,
    chunk = normalize_chunk(chunk, digest_envelope_ui_lines),
    active = active,
    visible = visible,
    armed = armed,
    point_count = point_count,
    automation_item_count = item_count,
    points = points,
    automation_items = items,
  }
end

-- Append an envelope once, keyed by its GUID, so an envelope that both the
-- track enumeration and the FX probe report is not hashed twice.
local function append_envelope(target, row, seen)
  if seen[row.guid] == true then return end
  seen[row.guid] = true
  target[#target + 1] = row
end

-- Collect the non-FX and FX envelopes that belong to one track. REAPER 7.84
-- reports the project tempo map -- which is owned by the master track -- as
-- envelope 0 of ordinary tracks as well, so ownership is verified through
-- Envelope_GetParentTrack instead of trusting the enumeration index.
local function track_envelopes(track)
  if type(reaper.Envelope_GetParentTrack) ~= "function" then
    return nil, "parent_api_unavailable"
  end
  local count = reaper.CountTrackEnvelopes(track)
  if not finite_number(count) or count < 0 then
    return nil, "envelope_count_unreadable"
  end
  local plain = json.array()
  local fx = json.array()
  local seen = {}
  for index = 0, count - 1 do
    local env = reaper.GetTrackEnvelope(track, index)
    if not env then return nil, "envelope_unreadable" end
    local parent, fx_index, parameter_index = reaper.Envelope_GetParentTrack(env)
    if parent == nil then return nil, "envelope_parent_unreadable" end
    if parent == track then
      local row, reason = envelope_context(env)
      if not row then return nil, reason end
      if seen[row.guid] ~= true then
        seen[row.guid] = true
        if fx_index ~= nil and fx_index >= 0 then
          row.fx_index = fx_index
          row.parameter_index = finite_or_zero(parameter_index)
          fx[#fx + 1] = row
        else
          plain[#plain + 1] = row
        end
      end
    end
  end
  return { plain = plain, fx = fx }
end

-- Take envelopes for one take. A take without envelopes returns an empty list;
-- an unreadable take envelope degrades the whole automation subgroup.
local function take_envelopes(take)
  if type(reaper.CountTakeEnvelopes) ~= "function" or
     type(reaper.GetTakeEnvelope) ~= "function" or
     type(reaper.Envelope_GetParentTake) ~= "function" then
    return nil, "take_envelope_api_unavailable"
  end
  local count = reaper.CountTakeEnvelopes(take)
  if not finite_number(count) or count < 0 then
    return nil, "take_envelope_count_unreadable"
  end
  local rows = json.array()
  local seen = {}
  for index = 0, count - 1 do
    local env = reaper.GetTakeEnvelope(take, index)
    if not env then return nil, "take_envelope_unreadable" end
    local parent = reaper.Envelope_GetParentTake(env)
    if parent == nil then return nil, "take_envelope_parent_unreadable" end
    if parent == take then
      local row, reason = envelope_context(env)
      if not row then return nil, reason end
      append_envelope(rows, row, seen)
    end
  end
  return rows
end

-- Probe every FX parameter for an existing envelope. GetFXEnvelope(..., false)
-- never creates one (verified: the project change count does not move), so this
-- is a pure read. The probe budget bounds the cost on very large chains.
local function fx_envelopes(track, track_guid, is_master, budget)
  if type(reaper.GetFXEnvelope) ~= "function" then
    return nil, "fx_envelope_api_unavailable"
  end
  local count = reaper.TrackFX_GetCount(track)
  if not finite_number(count) or count < 0 then return nil, "fx_count_unreadable" end
  local rows = json.array()
  for fx_index = 0, count - 1 do
    local parameter_count = reaper.TrackFX_GetNumParams(track, fx_index)
    if not finite_number(parameter_count) or parameter_count < 0 then
      return nil, "fx_parameter_count_unreadable"
    end
    for parameter_index = 0, parameter_count - 1 do
      budget.used = budget.used + 1
      if budget.used > budget.maximum then
        return nil, "fx_envelope_probe_limit"
      end
      local env = reaper.GetFXEnvelope(track, fx_index, parameter_index, false)
      if env then
        local row, reason = envelope_context(env)
        if not row then return nil, reason end
        row.track_guid = track_guid
        row.is_master = is_master
        row.fx_index = fx_index
        row.parameter_index = parameter_index
        rows[#rows + 1] = row
      end
    end
  end
  return rows
end

-- FX identity, order, bypass/offline state and every parameter value.
local function fx_digest(track)
  local count = reaper.TrackFX_GetCount(track)
  if not finite_number(count) or count < 0 then return nil, "fx_count_unreadable" end
  local rows = json.array()
  for fx_index = 0, count - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, fx_index, "")
    local parameter_count = reaper.TrackFX_GetNumParams(track, fx_index)
    if not finite_number(parameter_count) or parameter_count < 0 then
      return nil, "fx_parameter_count_unreadable"
    end
    local params = json.array()
    for parameter_index = 0, parameter_count - 1 do
      params[#params + 1] = {
        index = parameter_index,
        normalized = finite_or_zero(reaper.TrackFX_GetParamNormalized(
          track, fx_index, parameter_index)),
      }
    end
    rows[#rows + 1] = {
      index = fx_index,
      guid = reaper.TrackFX_GetFXGUID(track, fx_index) or "",
      name = name or "",
      enabled = flag_true(reaper.TrackFX_GetEnabled(track, fx_index)),
      offline = flag_true(reaper.TrackFX_GetOffline(track, fx_index)),
      params = params,
    }
  end
  return rows
end

local function send_digest(track)
  local count = reaper.GetTrackNumSends(track, 0)
  if not finite_number(count) or count < 0 then return nil, "send_count_unreadable" end
  local rows = json.array()
  for index = 0, count - 1 do
    local destination = reaper.GetTrackSendInfo_Value(track, 0, index, "P_DESTTRACK")
    rows[#rows + 1] = {
      index = index,
      dest_guid = destination and reaper.GetTrackGUID(destination) or "",
      volume = finite_or_zero(reaper.GetTrackSendInfo_Value(track, 0, index, "D_VOL")),
      pan = finite_or_zero(reaper.GetTrackSendInfo_Value(track, 0, index, "D_PAN")),
      pan_law = finite_or_zero(reaper.GetTrackSendInfo_Value(track, 0, index, "D_PANLAW")),
      mode = finite_or_zero(reaper.GetTrackSendInfo_Value(track, 0, index, "I_SENDMODE")),
      mute = flag_true(reaper.GetTrackSendInfo_Value(track, 0, index, "B_MUTE")),
      phase = flag_true(reaper.GetTrackSendInfo_Value(track, 0, index, "B_PHASE")),
      mono = flag_true(reaper.GetTrackSendInfo_Value(track, 0, index, "B_MONO")),
      source_channels = finite_or_zero(
        reaper.GetTrackSendInfo_Value(track, 0, index, "I_SRCCHAN")),
      destination_channels = finite_or_zero(
        reaper.GetTrackSendInfo_Value(track, 0, index, "I_DSTCHAN")),
      midi_flags = finite_or_zero(
        reaper.GetTrackSendInfo_Value(track, 0, index, "I_MIDIFLAGS")),
    }
  end
  return rows
end

-- Hardware outputs (category 1) are routing that changes the delivered mix, so
-- they belong in the digest next to ordinary sends.
local function hardware_output_digest(track)
  local count = reaper.GetTrackNumSends(track, 1)
  if not finite_number(count) or count < 0 then
    return nil, "hardware_output_count_unreadable"
  end
  local rows = json.array()
  for index = 0, count - 1 do
    local function value(name)
      return finite_or_zero(reaper.GetTrackSendInfo_Value(track, 1, index, name))
    end
    rows[#rows + 1] = {
      index = index,
      volume = value("D_VOL"),
      pan = value("D_PAN"),
      pan_law = value("D_PANLAW"),
      mute = flag_true(value("B_MUTE")),
      phase = flag_true(value("B_PHASE")),
      mono = flag_true(value("B_MONO")),
      source_channels = value("I_SRCCHAN"),
      destination_channels = value("I_DSTCHAN"),
      send_mode = value("I_SENDMODE"),
    }
  end
  return rows
end

-- Serialized FX state. GetTrackStateChunk is the only documented way to read a
-- plug-in's opaque state (TrackFX_GetFXChunk does not exist in the 7.85
-- catalog), so the <FXCHAIN> block is copied out of the track state chunk and
-- hashed through the digest fingerprint. If a chain cannot be read the whole
-- fx_state subgroup is reported missing rather than silently downgraded to
-- parameter values only.
local function fx_chain_state(track)
  if type(reaper.GetTrackStateChunk) ~= "function" then
    return nil, "track_chunk_api_unavailable"
  end
  local fx_count = reaper.TrackFX_GetCount(track)
  if not finite_number(fx_count) or fx_count < 0 then
    return nil, "fx_count_unreadable"
  end
  local ok, chunk = reaper.GetTrackStateChunk(track, "", false)
  if not ok or type(chunk) ~= "string" then return nil, "track_chunk_unreadable" end
  -- The same project state is spelled <FXCHAIN for tracks and master alike in
  -- REAPER 7.84, while the saved .rpp writes the master chain as
  -- <MASTERFXLIST. Accept either so a representation change degrades nothing.
  local block = chunk_block(chunk, "FXCHAIN")
  if not block then block = chunk_block(chunk, "MASTERFXLIST") end
  if not block then
    if fx_count == 0 then return { fx_count = 0, chunk = "" } end
    return nil, "fx_chain_absent"
  end
  local normalized = normalize_chunk(block, digest_fx_chain_ui_lines)
  if #normalized > digest_max_fx_chain_chars then
    return nil, "fx_chain_too_large"
  end
  return { fx_count = fx_count, chunk = normalized }
end

local function master_digest(track)
  local fx, fx_reason = fx_digest(track)
  if not fx then return nil, fx_reason end
  local sends, send_reason = send_digest(track)
  if not sends then return nil, send_reason end
  local hardware, hardware_reason = hardware_output_digest(track)
  if not hardware then return nil, hardware_reason end
  local mute = reaper.GetMediaTrackInfo_Value(track, "B_MUTE")
  return {
    guid = reaper.GetTrackGUID(track) or "",
    is_master = true,
    name = get_track_name(track),
    volume = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_VOL")),
    pan = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_PAN")),
    mute = mute ~= nil and mute ~= 0,
    solo = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "I_SOLO")),
    channel_count = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "I_NCHAN")),
    pan_mode = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "I_PANMODE")),
    pan_law = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_PANLAW")),
    pan_law_flags = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_PANLAW_FLAGS")),
    width = finite_or_zero(reaper.GetMediaTrackInfo_Value(track, "D_WIDTH")),
    dualpan_left = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_DUALPANL")),
    dualpan_right = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_DUALPANR")),
    phase_inverted = flag_true(
      reaper.GetMediaTrackInfo_Value(track, "B_PHASE")),
    main_send = flag_true(reaper.GetMediaTrackInfo_Value(track, "B_MAINSEND")),
    play_offset = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "D_PLAY_OFFSET")),
    play_offset_flags = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_PLAY_OFFSET_FLAG")),
    freeze_count = finite_or_zero(
      reaper.GetMediaTrackInfo_Value(track, "I_FREEZECOUNT")),
    fx = fx,
    sends = sends,
    hardware_outputs = hardware,
  }
end

local function project_settings_digest(project)
  local settings = {}
  local unread = json.array()
  for _, name in ipairs(digest_project_settings) do
    local value = reaper.GetSetProjectInfo(project, name, 0, false)
    if finite_number(value) then
      settings[name:lower()] = value
    else
      unread[#unread + 1] = name
    end
  end
  return { settings = settings, unread = unread, complete = #unread == 0 }
end

local function project_digest(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity_state(project, path)
  local tracks = json.array()
  local item_rows = {}
  local item_state_chunks = {}
  local item_state_reasons = json.array()
  local automation_tracks = json.array()
  local automation_items = json.array()
  local automation_fx = json.array()
  local fx_state_tracks = json.array()
  local automation_gaps = json.array()
  local fx_state_gaps = json.array()
  local seen_envelope = {}
  local fx_ok = true
  local sends_ok = true
  local automation_ok = true
  local fx_state_ok = true
  local items_scanned = 0
  local budget = { used = 0, maximum = digest_max_fx_envelope_probes }
  local track_count = reaper.CountTracks(project)
  for index = 0, track_count - 1 do
    local track = reaper.GetTrack(project, index)
    local track_guid = reaper.GetTrackGUID(track) or ""
    local context = track_context(project, track, index,
                                  { include_items = true,
                                    include_fx_params = true })
    local fx, fx_reason = fx_digest(track)
    if not fx then
      fx_ok = false
      fx = json.array()
      automation_gaps[#automation_gaps + 1] = { track_guid = track_guid,
                                                reason = fx_reason }
    end
    local sends, send_reason = send_digest(track)
    if not sends then
      sends_ok = false
      sends = json.array()
      automation_gaps[#automation_gaps + 1] = { track_guid = track_guid,
                                                reason = send_reason }
    end
    local hardware, hardware_reason = hardware_output_digest(track)
    if not hardware then
      sends_ok = false
      hardware = json.array()
      automation_gaps[#automation_gaps + 1] = { track_guid = track_guid,
                                                reason = hardware_reason }
    end
    local envelopes, envelope_reason = track_envelopes(track)
    local track_envelope_rows = json.array()
    if envelopes then
      for row_index = 1, #envelopes.plain do
        local row = envelopes.plain[row_index]
        append_envelope(track_envelope_rows, row, seen_envelope)
      end
      for row_index = 1, #envelopes.fx do
        local row = envelopes.fx[row_index]
        row.track_guid = track_guid
        row.is_master = false
        append_envelope(automation_fx, row, seen_envelope)
      end
    else
      automation_ok = false
      automation_gaps[#automation_gaps + 1] = { track_guid = track_guid,
                                                reason = envelope_reason }
    end
    local fx_envelope_rows, fx_envelope_reason =
      fx_envelopes(track, track_guid, false, budget)
    if fx_envelope_rows then
      for row_index = 1, #fx_envelope_rows do
        append_envelope(automation_fx, fx_envelope_rows[row_index], seen_envelope)
      end
    else
      automation_ok = false
      automation_gaps[#automation_gaps + 1] = { track_guid = track_guid,
                                                reason = fx_envelope_reason }
    end
    automation_tracks[#automation_tracks + 1] = {
      track_guid = track_guid,
      envelopes = track_envelope_rows,
    }
    local chain, chain_reason = fx_chain_state(track)
    if chain then
      fx_state_tracks[#fx_state_tracks + 1] = {
        track_guid = track_guid, is_master = false, fx_count = chain.fx_count,
        chunk = chain.chunk, complete = true,
      }
    else
      fx_state_ok = false
      fx_state_tracks[#fx_state_tracks + 1] = {
        track_guid = track_guid, is_master = false, fx_count = -1,
        chunk = "", complete = false,
      }
      fx_state_gaps[#fx_state_gaps + 1] = { track_guid = track_guid,
                                            reason = chain_reason }
    end
    tracks[#tracks + 1] = {
      guid = track_guid,
      volume = finite_or_zero(context.volume),
      pan = finite_or_zero(context.pan),
      mute = context.mute == true,
      solo = finite_or_zero(context.solo),
      channel_count = finite_or_zero(context.channel_count),
      pan_mode = finite_or_zero(context.pan_mode),
      pan_law = finite_or_zero(context.pan_law),
      pan_law_flags = finite_or_zero(context.pan_law_flags),
      width = finite_or_zero(context.width),
      dualpan_left = finite_or_zero(context.dualpan_left),
      dualpan_right = finite_or_zero(context.dualpan_right),
      phase_inverted = context.phase_inverted == true,
      main_send = context.main_send == true,
      play_offset = finite_or_zero(context.play_offset),
      play_offset_flags = finite_or_zero(context.play_offset_flags),
      freeze_count = finite_or_zero(context.freeze_count),
      folder_depth = finite_or_zero(context.folder_depth),
      fx = fx,
      sends = sends,
      hardware_outputs = hardware,
    }
    for item_index = 1, #(context.items or {}) do
      local source_item = context.items[item_index]
      local record = {
        guid = source_item.guid or "",
        track_guid = context.guid or "",
        position_s = finite_or_zero(source_item.position_s),
        length_s = finite_or_zero(source_item.length_s),
        start_offset_s = finite_or_zero(source_item.start_offset_s),
        playrate = finite_or_zero(source_item.playrate),
        mute = source_item.mute == true,
        fade_in_s = finite_or_zero(source_item.fade_in_s),
        fade_out_s = finite_or_zero(source_item.fade_out_s),
        channel_mode = finite_or_zero(source_item.channel_mode),
        group_id = finite_or_zero(source_item.group_id),
        take_guid = "",
        source_path = "",
        take_volume = 1,
        take_pan = 0,
        take_pitch = 0,
        preserve_pitch = false,
        loop_source = source_item.loop_source == true,
        beat_attach_mode = finite_or_zero(source_item.beat_attach_mode),
        stretch_marker_positions = json.array(),
        state_chunk = "",
        state_chunk_complete = false,
      }
      if type(source_item.active_take) == "table" and
         source_item.active_take ~= json.null then
        record.take_guid = source_item.active_take.guid or ""
        record.source_path = source_item.active_take.source or ""
        record.take_volume = finite_or_zero(source_item.active_take.volume)
        record.take_pan = finite_or_zero(source_item.active_take.pan)
        record.take_pitch = finite_or_zero(source_item.active_take.pitch)
        record.preserve_pitch = source_item.active_take.preserve_pitch == true
      end
      for marker_index = 1, #source_item.stretch_marker_positions do
        local marker = source_item.stretch_marker_positions[marker_index]
        record.stretch_marker_positions[#record.stretch_marker_positions + 1] = {
          pos = finite_or_zero(marker.pos),
          srcpos = finite_or_zero(marker.srcpos),
        }
      end
      item_rows[#item_rows + 1] = { record = record, track_index = index,
                                    item_index = item_index - 1 }
    end
    for item_index = 0, reaper.CountTrackMediaItems(track) - 1 do
      local item = reaper.GetTrackMediaItem(track, item_index)
      if item then
        local item_guid_ok, item_guid = reaper.GetSetMediaItemInfo_String(
          item, "GUID", "", false)
        -- The full item state chunk is the only documented read that covers
        -- take FX, take pitch/volume/pan, fades and source flags together. It
        -- is normalized so arrange-view selection cannot look like a signal
        -- change, and hashed by the Python reader so the digest stays small.
        -- Read it for every item, including items without an active take: an
        -- item with no take still has geometry, fades and item FX, and a
        -- missing chunk must not make every later capture look incomplete.
        if type(reaper.GetItemStateChunk) == "function" then
          local chunk_ok, chunk = reaper.GetItemStateChunk(item, "", false)
          if chunk_ok and type(chunk) == "string" then
            local normalized = normalize_chunk(chunk, digest_item_ui_lines)
            if #normalized > digest_max_fx_chain_chars then
              item_state_reasons[#item_state_reasons + 1] = {
                item_guid = item_guid_ok and item_guid or "",
                reason = "item_state_chunk_too_large",
              }
            else
              item_state_chunks[item_guid_ok and item_guid or ""] = normalized
            end
          else
            item_state_reasons[#item_state_reasons + 1] = {
              item_guid = item_guid_ok and item_guid or "",
              reason = "item_state_chunk_unreadable",
            }
          end
        else
          item_state_reasons[#item_state_reasons + 1] = {
            item_guid = item_guid_ok and item_guid or "",
            reason = "item_state_chunk_api_unavailable",
          }
        end
        local take = reaper.GetActiveTake(item)
        if take then
          items_scanned = items_scanned + 1
          local take_guid_ok, take_guid = reaper.GetSetMediaItemTakeInfo_String(
            take, "GUID", "", false)
          local take_rows, take_reason = take_envelopes(take)
          if take_rows then
            for row_index = 1, #take_rows do
              local row = take_rows[row_index]
              row.track_guid = track_guid
              row.item_guid = item_guid_ok and item_guid or ""
              row.take_guid = take_guid_ok and take_guid or ""
              append_envelope(automation_items, row, seen_envelope)
            end
          else
            automation_ok = false
            automation_gaps[#automation_gaps + 1] = {
              track_guid = track_guid,
              item_guid = item_guid_ok and item_guid or "",
              reason = take_reason,
            }
          end
        end
      end
    end
  end
  -- Stable order: items by project position, ties by track then item index;
  -- tempo markers by time, ties by marker index.
  table.sort(item_rows, function(left, right)
    if left.record.position_s ~= right.record.position_s then
      return left.record.position_s < right.record.position_s
    end
    if left.track_index ~= right.track_index then
      return left.track_index < right.track_index
    end
    return left.item_index < right.item_index
  end)
  local items = json.array()
  for index = 1, #item_rows do
    local record = item_rows[index].record
    local chunk = item_state_chunks[record.guid or ""]
    if type(chunk) == "string" then
      record.state_chunk = chunk
      record.state_chunk_complete = true
    end
    items[#items + 1] = record
  end

  local tempo_rows = {}
  local tempo_count = reaper.CountTempoTimeSigMarkers(project)
  for index = 0, (finite_number(tempo_count) and tempo_count or 0) - 1 do
    local ok, time_s, _, _, marker_bpm, numerator, denominator, linear =
      reaper.GetTempoTimeSigMarker(project, index)
    if ok then
      tempo_rows[#tempo_rows + 1] = {
        time_s = finite_or_zero(time_s), bpm = finite_or_zero(marker_bpm),
        numerator = finite_or_zero(numerator),
        denominator = finite_or_zero(denominator),
        linear = flag_true(linear), marker_index = index,
      }
    end
  end
  table.sort(tempo_rows, function(left, right)
    if left.time_s ~= right.time_s then return left.time_s < right.time_s end
    return left.marker_index < right.marker_index
  end)
  local tempo = json.array()
  for index = 1, #tempo_rows do
    local row = tempo_rows[index]
    tempo[#tempo + 1] = { time_s = row.time_s, bpm = row.bpm,
                          numerator = row.numerator,
                          denominator = row.denominator,
                          linear = row.linear }
  end

  local master = reaper.GetMasterTrack(project)
  local master_guid = reaper.GetTrackGUID(master) or ""
  local master_row, master_reason = master_digest(master)
  local master_ok = master_row ~= nil
  if not master_ok then
    -- The master's FX, routing and level could not be read as one block, so the
    -- per-track fx coverage flag cannot claim the whole project either.
    fx_ok = false
    automation_gaps[#automation_gaps + 1] = { track_guid = master_guid,
                                              is_master = true,
                                              reason = master_reason }
  end
  local master_envelope_rows = json.array()
  local master_envelopes, master_envelope_reason = track_envelopes(master)
  if master_envelopes then
    for row_index = 1, #master_envelopes.plain do
      append_envelope(master_envelope_rows, master_envelopes.plain[row_index],
                      seen_envelope)
    end
    for row_index = 1, #master_envelopes.fx do
      local row = master_envelopes.fx[row_index]
      row.track_guid = master_guid
      row.is_master = true
      append_envelope(automation_fx, row, seen_envelope)
    end
  else
    automation_ok = false
    automation_gaps[#automation_gaps + 1] = { track_guid = master_guid,
                                              is_master = true,
                                              reason = master_envelope_reason }
  end
  local master_fx_envelopes, master_fx_reason =
    fx_envelopes(master, master_guid, true, budget)
  if master_fx_envelopes then
    for row_index = 1, #master_fx_envelopes do
      append_envelope(automation_fx, master_fx_envelopes[row_index], seen_envelope)
    end
  else
    automation_ok = false
    automation_gaps[#automation_gaps + 1] = { track_guid = master_guid,
                                              is_master = true,
                                              reason = master_fx_reason }
  end
  local master_chain, master_chain_reason = fx_chain_state(master)
  if master_chain then
    fx_state_tracks[#fx_state_tracks + 1] = {
      track_guid = master_guid, is_master = true,
      fx_count = master_chain.fx_count, chunk = master_chain.chunk,
      complete = true,
    }
  else
    fx_state_ok = false
    fx_state_tracks[#fx_state_tracks + 1] = {
      track_guid = master_guid, is_master = true, fx_count = -1,
      chunk = "", complete = false,
    }
    fx_state_gaps[#fx_state_gaps + 1] = { track_guid = master_guid,
                                          is_master = true,
                                          reason = master_chain_reason }
  end

  local project_row = project_settings_digest(project)
  local project_ok = project_row.complete

  local missing = json.array()
  if not fx_ok then missing[#missing + 1] = "fx" end
  if not sends_ok then missing[#missing + 1] = "sends" end
  if not automation_ok then missing[#missing + 1] = "automation" end
  if not master_ok then missing[#missing + 1] = "master" end
  if not project_ok then missing[#missing + 1] = "project" end
  if not fx_state_ok then missing[#missing + 1] = "fx_state" end

  local fields = {
    tracks = tracks,
    items = items,
    tempo = tempo,
    automation = {
      tracks = automation_tracks,
      master = master_envelope_rows,
      items = automation_items,
      fx = automation_fx,
      items_scanned = items_scanned,
      complete = automation_ok,
      gaps = automation_gaps,
    },
    fx_state = {
      algorithm = "rpp_fxchain_text_v1",
      ui_lines_removed = json.array({ "WNDRECT", "SHOW", "LASTSEL", "DOCKED",
                                      "FLOATPOS", "WAK", "MULTIFXLINK" }),
      tracks = fx_state_tracks,
      complete = fx_state_ok,
      gaps = fx_state_gaps,
    },
    project = project_row,
    item_state = {
      complete = #item_state_reasons == 0,
      scanned = items_scanned,
      reasons = item_state_reasons,
    },
  }
  if master_ok then fields.master = master_row end
  return {
    project_id = revision.project_id,
    path = revision.path,
    change_count = revision.change_count,
    digest_version = digest_version,
    coverage = { tracks = true, items = true, takes = true,
                 fx = fx_ok, sends = sends_ok, tempo = true,
                 master = master_ok, automation = automation_ok,
                 envelopes = automation_ok, project = project_ok,
                 fx_state = fx_state_ok },
    missing = missing,
    fields = fields,
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
  local revision = project_identity_state(project, path)
  local source = tempo_marker_count == 0 and "project.base_tempo_no_markers" or
                 "TimeMap_GetTimeSigAtTime"
  return { project_id = revision.project_id, revision = revision,
           points = points, source = source }
end

local function project_render_settings_read(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity_state(project, path)
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

local function project_set_track_mutes(arguments)
  local project, path = target_project(arguments)
  local expected = arguments.expected_revision
  local revision = project_identity_state(project, path)
  if not same_revision_fields(expected, revision) then
    fail("stale_context", "Project changed before the batched mute operation",
         { expected = expected, current = revision })
  end
  local mutes = arguments.mutes
  if type(mutes) ~= "table" or #mutes == 0 then
    fail("validation_error", "mutes must be a non-empty array")
  end
  local applied = json.array()
  for index = 1, #mutes do
    local entry = mutes[index]
    if type(entry) ~= "table" or type(entry.guid) ~= "string" or
       entry.guid == "" or type(entry.mute) ~= "boolean" then
      fail("validation_error", "Each mute entry needs a GUID and a boolean mute",
           { index = index })
    end
    local track = resolve_track(project, entry.guid)
    if not track then
      fail("not_found", "A batched mute target is not in the project",
           { guid = entry.guid })
    end
    reaper.SetMediaTrackInfo_Value(track, "B_MUTE", entry.mute and 1 or 0)
    applied[#applied + 1] = {
      guid = entry.guid,
      mute = flag_true(reaper.GetMediaTrackInfo_Value(track, "B_MUTE")),
    }
  end
  local after = project_identity_state(project, path)
  return { project_id = after.project_id, revision = after,
           project_after = after, mutes = applied }
end

local unresolved_sources

local function project_capture_render(arguments)
  local project, path = target_project(arguments)
  local previous_active = select(1, reaper.EnumProjects(-1, ""))
  local switched_active = false
  if previous_active ~= project then
    local select_ok, selected = pcall(reaper.SelectProjectInstance, project)
    if not select_ok or selected == false then
      fail("api_unavailable", "REAPER could not activate the target project for rendering",
           { project_id = path or "" })
    end
    switched_active = true
  end
  local expected = arguments.expected_revision
  local master_only = arguments.master_only == true
  local selected_tracks_via_master = arguments.selected_tracks_via_master == true
  local parallel_via_master = arguments.parallel_via_master == true
  local probe_mode = arguments.probe_mode == true
  local render_settings_override = arguments.render_settings
  if render_settings_override == json.null then render_settings_override = nil end
  if render_settings_override ~= nil and
     (not finite_number(render_settings_override) or render_settings_override < 0 or
      render_settings_override % 1 ~= 0) then
    fail("validation_error", "render_settings must be a non-negative integer")
  end
  if parallel_via_master and not selected_tracks_via_master then
    fail("validation_error", "parallel_via_master requires selected_tracks_via_master")
  end
  -- Optional additive contract: render only these track GUIDs (a bounded batch)
  -- instead of every project track.  Selection is still restored in full, and
  -- the one-file/one-GUID mapping below is enforced against this exact set.
  local track_guids = arguments.track_guids
  if track_guids == json.null then track_guids = nil end
  local subset, subset_count = nil, 0
  if track_guids ~= nil then
    if type(track_guids) ~= "table" then
      fail("validation_error", "track_guids must be an array of track GUID strings")
    end
    if master_only then
      fail("validation_error", "track_guids cannot be combined with master_only")
    end
    subset = {}
    for _, guid in ipairs(track_guids) do
      if type(guid) ~= "string" or guid == "" then
        fail("validation_error", "track_guids entries must be non-empty strings")
      end
      if subset[guid] then
        fail("validation_error", "track_guids must not repeat a track GUID",
             { guid = guid })
      end
      subset[guid] = true
      subset_count = subset_count + 1
    end
    if subset_count == 0 then
      fail("validation_error", "track_guids must name at least one track")
    end
  end
  local channels_name = arguments.channels
  if channels_name == nil or channels_name == json.null then channels_name = "stereo" end
  if channels_name ~= "stereo" and channels_name ~= "mono" then
    fail("validation_error", "channels must be \"stereo\" or \"mono\"",
         { channels = channels_name })
  end
  local render_channels = channels_name == "mono" and 1 or 2
  local sample_rate = arguments.sample_rate
  if sample_rate == nil or sample_rate == json.null then sample_rate = 0 end
  if not finite_number(sample_rate) or sample_rate < 0 or sample_rate > 768000 then
    fail("validation_error",
         "sample_rate must be 0 (project rate) or a finite rate up to 768000",
         { sample_rate = sample_rate })
  end
  local tail_ms = arguments.tail_ms
  if tail_ms == nil or tail_ms == json.null then tail_ms = 0 end
  if not finite_number(tail_ms) or tail_ms < 0 or tail_ms > 3600000 then
    fail("validation_error", "tail_ms must be a finite number of milliseconds >= 0",
         { tail_ms = tail_ms })
  end
  local revision = project_identity_state(project, path)
  if not same_revision_fields(expected, revision) then
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
                           "RENDER_TAILFLAG", "RENDER_TAILMS",
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
  local prior_transport = {
    play_state = reaper.GetPlayStateEx(project),
    cursor_s = reaper.GetCursorPositionEx(project),
    edit_cursor_s = reaper.GetCursorPositionEx(project),
    play_position_s = reaper.GetPlayPositionEx(project),
  }
  local restored_transport = {}
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
    local cursor_ok = pcall(reaper.SetEditCurPos2, project,
                             prior_transport.cursor_s, false, false)
    restored_transport = {
      play_state = reaper.GetPlayStateEx(project),
      cursor_s = reaper.GetCursorPositionEx(project),
      edit_cursor_s = reaper.GetCursorPositionEx(project),
      play_position_s = reaper.GetPlayPositionEx(project),
      play_position_observed = true,
      play_position_restorable = false,
      position_verified = "edit_cursor_s",
    }
    if not cursor_ok or math.abs(restored_transport.cursor_s -
                                 prior_transport.cursor_s) > 1e-6 then
      failures[#failures + 1] = "edit_cursor"
    end
    if restored_transport.play_state ~= prior_transport.play_state then
      failures[#failures + 1] = "play_state"
    end
    if switched_active and previous_active then
      local active_ok = pcall(reaper.SelectProjectInstance, previous_active)
      if not active_ok then failures[#failures + 1] = "active_project_tab" end
    end
    return failures
  end

  local ok, capture = xpcall(function()
    local media_issues = unresolved_sources(project)
    if #media_issues > 0 then
      fail("media_unresolved",
           "Some take sources could not be resolved; the analysis render would be silent",
           { sources = media_issues })
    end
    local subset_found = 0
    for index = 0, track_count - 1 do
      local track = reaper.GetTrack(project, index)
      tracks[index + 1] = track
      selected[index + 1] = reaper.IsTrackSelected(track)
      -- Master-mix renders do not need a selected-track target.  Selecting
      -- every track here made the master-only path depend on selection state;
      -- leave the user's selection untouched for that mode.
      if not master_only then
        local wanted = true
        if subset ~= nil then
          local guid = reaper.GetTrackGUID(track)
          wanted = subset[guid] == true
          if wanted then subset_found = subset_found + 1 end
        end
        reaper.SetTrackSelected(track, wanted)
      end
    end
    if subset ~= nil and subset_found ~= subset_count then
      fail("validation_error",
           "track_guids names a track that is not in this project",
           { requested = subset_count, found = subset_found })
    end
    local target_track_count = track_count
    if subset ~= nil then target_track_count = subset_count end

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
    local render_settings = 5
    if master_only then
      render_settings = 0
    elseif selected_tracks_via_master then
      render_settings = 128
      if parallel_via_master then render_settings = render_settings + (8 << 16) end
    end
    if render_settings_override ~= nil then
      render_settings = render_settings_override
      if parallel_via_master then render_settings = render_settings + (8 << 16) end
    end
    local includes_master = master_only or (render_settings % 2 == 1)
    set_number("RENDER_SETTINGS", render_settings)
    if has_section then
      local range_ok = reaper.GetSet_LoopTimeRange2(
        project, true, false, section_start, section_end, false)
      if not range_ok then error("REAPER rejected the analysis time selection") end
      set_number("RENDER_BOUNDSFLAG", 2) -- time selection
    else
      set_number("RENDER_BOUNDSFLAG", 1) -- entire project
    end
    set_number("RENDER_CHANNELS", render_channels)
    set_number("RENDER_SRATE", sample_rate) -- 0 means project sample rate
    if tail_ms > 0 then
      -- REAPER 7 documents RENDER_TAILFLAG as a per-bounds bitmask
      -- (&1=custom bounds, &2=entire project, &4=time selection), not a
      -- boolean: a literal 1 would add no tail to the bounds rendered here.
      local bounds_flag = has_section and 2 or 1
      set_number("RENDER_TAILFLAG", 2 ^ bounds_flag)
      set_number("RENDER_TAILMS", tail_ms)
    else
      set_number("RENDER_TAILFLAG", 0)
      set_number("RENDER_TAILMS", 0)
    end
    set_number("RENDER_ADDTOPROJ", 0)
    set_number("RENDER_DITHER", 16) -- disable render dither/noise shaping
    set_number("RENDER_NORMALIZE", 0) -- no normalize/limit/fade postprocessing

    -- Read back what the render will actually use, so the capture record
    -- describes the settings rather than the request.
    local settings_used = {}
    for _, name in ipairs({ "RENDER_SETTINGS", "RENDER_BOUNDSFLAG",
                            "RENDER_CHANNELS", "RENDER_SRATE",
                            "RENDER_TAILFLAG", "RENDER_TAILMS",
                            "RENDER_ADDTOPROJ" }) do
      local value = reaper.GetSetProjectInfo(project, name, 0, false)
      if not finite_number(value) then
        error("REAPER returned an invalid " .. name .. " after render setup")
      end
      settings_used[name] = value
    end
    local effective_sample_rate = settings_used.RENDER_SRATE
    local sample_rate_source = "render_setting"
    if effective_sample_rate <= 0 then
      -- RENDER_SRATE=0 defers to the project rate; report that rate and label
      -- where it came from instead of implying a measured render rate.
      sample_rate_source = "project_srate"
      local rate_ok, project_rate = pcall(reaper.GetSetProjectInfo, project,
                                          "PROJECT_SRATE", 0, false)
      if rate_ok and finite_number(project_rate) and project_rate > 0 then
        effective_sample_rate = project_rate
      else
        effective_sample_rate = 0
        sample_rate_source = "unknown"
      end
    end

    local targets_ok, targets_text = reaper.GetSetProjectInfo_String(
      project, "RENDER_TARGETS", "", false)
    local target_source = "reaper_render_targets"
    if not targets_ok or type(targets_text) ~= "string" or targets_text == "" then
      if not probe_mode then
        error("REAPER could not build the analysis render target list: ok=" ..
              tostring(targets_ok) .. ", type=" .. type(targets_text) ..
              ", value=" .. tostring(targets_text) ..
              ", RENDER_SETTINGS=" .. tostring(reaper.GetSetProjectInfo(project, "RENDER_SETTINGS", 0, false)))
      end
      target_source = "scanned_after_render"
      capture_targets = {}
    else
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
    end
    local expected_target_count = master_only and 1 or
                                  (includes_master and target_track_count + 1 or
                                   target_track_count)
    if #capture_targets > 0 and #capture_targets ~= expected_target_count then
      error("REAPER did not produce the expected analysis render targets: expected " ..
            tostring(expected_target_count) .. ", got " .. tostring(#capture_targets))
    end

    local action_name = reaper.kbd_getTextFromCmd(
      42230, reaper.SectionFromUniqueID(0))
    if type(action_name) ~= "string" or not action_name:lower():find("render project", 1, true) then
      error("The installed REAPER render action 42230 is unavailable")
    end
    reaper.Main_OnCommandEx(42230, 0, project)

    if #capture_targets == 0 then
      local index = 0
      while true do
        local name = reaper.EnumerateFiles(output_dir, index)
        if not name then break end
        if name:lower():match("%.wav$") and name:sub(1, #prefix) == prefix then
          capture_targets[#capture_targets + 1] = output_dir .. "/" .. name
        end
        index = index + 1
      end
      if #capture_targets ~= expected_target_count then
        error("Scanned render directory did not contain the expected files: expected " ..
              tostring(expected_target_count) .. ", got " .. tostring(#capture_targets))
      end
    end

    local files, seen_tracks, seen_track_count, master_count = json.array(), {}, 0, 0
    for _, target in ipairs(capture_targets) do
      local name = target:match("([^/\\]+)$") or ""
      local rest = name:sub(#prefix + 1)
      -- $tracknumber-$track loses its separator when the track name is empty,
      -- so the render lands as "<prefix>33.wav". A number-only match keeps that
      -- track mapped instead of counting it as an extra master file.
      local number = tonumber(rest:match("^(%d+)")) or 0
      local track_guid, track_name, track_index, is_master
      if number >= 1 and number <= track_count then
        local track = reaper.GetTrack(project, number - 1)
        track_guid = reaper.GetTrackGUID(track)
        if subset ~= nil and subset[track_guid] ~= true then
          error("REAPER rendered a track outside the requested capture subset: " .. target)
        end
        local name_ok
        name_ok, track_name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
        if not name_ok then error("Could not read rendered track name") end
        -- An unnamed track is legitimate; the render row still needs a label,
        -- and an empty one is rejected by the client contract.
        if track_name == "" then track_name = "Track " .. tostring(number) end
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
    local expected_master_count = master_only and 1 or (includes_master and 1 or 0)
    if master_count ~= expected_master_count or
       (not master_only and seen_track_count ~= target_track_count) then
      -- A mismatch is a hard failure, but the counts alone cannot say which
      -- target was wrong. Return the whole mapping evidence so the caller can
      -- name the missing track or the extra master instead of guessing.
      local expected_rows = json.array()
      for index = 0, track_count - 1 do
        local track = reaper.GetTrack(project, index)
        local guid = reaper.GetTrackGUID(track)
        if subset == nil or subset[guid] == true then
          local name_ok, track_name_value =
            reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
          local selected = reaper.IsTrackSelected(track)
          expected_rows[#expected_rows + 1] = {
            guid = guid, number = index + 1, index = index,
            name = name_ok and track_name_value or "",
            selected = selected,
            item_count = reaper.CountTrackMediaItems(track),
          }
        end
      end
      local actual_rows = json.array()
      for _, target in ipairs(capture_targets) do
        local file_name = target:match("([^/\\]+)$") or ""
        local rest = file_name:sub(#prefix + 1)
        local bytes = -1
        local handle = io.open(target, "rb")
        if handle then bytes = handle:seek("end") or -1; handle:close() end
        actual_rows[#actual_rows + 1] = {
          path = target, file = file_name,
          number = tonumber(rest:match("^(%d+)")) or 0,
          bytes = bytes,
        }
      end
      fail("render_mapping_mismatch",
           "Rendered files could not be mapped one-to-one to requested tracks " ..
           "and master: tracks=" .. tostring(seen_track_count) .. "/" ..
           tostring(target_track_count) .. ", masters=" .. tostring(master_count) ..
           "/" .. tostring(expected_master_count),
           { tracks_mapped = seen_track_count, tracks_expected = target_track_count,
             masters_mapped = master_count, masters_expected = expected_master_count,
             job_id = job_id, prefix = prefix, output_dir = output_dir,
             master_only = master_only, includes_master = includes_master,
             selected_tracks_via_master = selected_tracks_via_master,
             parallel_via_master = parallel_via_master,
             subset_count = subset_count or 0, project_track_count = track_count,
             settings = settings_used, targets = capture_targets,
             expected = expected_rows, actual = actual_rows })
    end
    local capture_point = master_only and "native_render_master_only" or
                          (selected_tracks_via_master and
                           "native_render_selected_tracks_via_master" or
                           "native_render_track_stems_plus_master")
    return { project_id = revision.project_id, revision = revision,
             files = files, capture_point = capture_point,
             selected_tracks_via_master = selected_tracks_via_master,
             parallel_via_master = parallel_via_master,
             render_settings_requested = render_settings,
             master_included = includes_master,
             render_action = action_name,
             render_settings_used = settings_used,
             render_targets = capture_targets,
             target_count = #capture_targets,
             target_track_count = target_track_count,
             track_guids = track_guids,
             target_source = target_source,
             channels = render_channels,
             sample_rate = effective_sample_rate,
             sample_rate_source = sample_rate_source,
             tail_s = tail_ms / 1000 }
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
  capture.restore_report = { restored = true, failures = json.array(),
                            dirty_before = prior_dirty,
                            dirty_after = reaper.GetSetProjectInfo(project, "DIRTY", 0, false),
                            time_selection_before = { start_s = prior_time_start,
                                                      end_s = prior_time_end },
                            time_selection_after = (function()
                              local start_s, end_s = reaper.GetSet_LoopTimeRange2(
                                project, false, false, 0, 0, false)
                              return { start_s = start_s, end_s = end_s }
                            end)(),
                            transport_position_before_s = prior_transport.edit_cursor_s,
                            transport_position_after_s = restored_transport.edit_cursor_s,
                            transport_position_kind = "edit_cursor_s",
                            play_position_semantics = "observed_playback_not_project_cursor",
                            transport_before = prior_transport,
                            transport_after = restored_transport }
  capture.revision_before = revision
  capture.revision_after = project_identity_state(project, path)
  return capture
end

local function projects_list()
  local projects, index = json.array(), 0
  while true do
    local project, path = reaper.EnumProjects(index, "")
    if not project then break end
    local identity = project_identity_state(project, path or "")
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
    bridge_build = bridge_build,
    reaper_version = reaper.GetAppVersion(),
    started_epoch = tonumber(bridge_session_id:match("^(%x+)"), 16),
    last_seen_epoch = os.time(),
    active_project = project_identity_state(active, active_path),
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
  local before = before_path and project_identity_state(call_project, before_path) or nil
  local packed = table.pack(pcall(fn, table.unpack(call_args, 1, call_args.n)))
  if not packed[1] then fail("api_call_failed", tostring(packed[2]), { name = name }) end
  local after_path = project_path_for(call_project)
  local after = after_path and project_identity_state(call_project, after_path) or json.null
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
  local current = project_identity_state(project, path)
  if not same_revision_fields(expected, current) then
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
  return same_revision_fields(left, right)
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
    -- recFX must be false: true addresses the record-input/monitoring chain,
    -- where the FX is invisible to TrackFX_GetCount and the mix. A position
    -- is expressed as a negative instantiate value (-1000 = first slot).
    local instantiate = -1
    if finite_number(values.position) then
      instantiate = -1000 - values.position
    end
    local index = reaper.TrackFX_AddByName(target, values.name, false,
                                           instantiate)
    if (not finite_number(index) or index < 0) and values.position == nil then
      -- A cold VST cache can miss on the first lookup; one retry costs nothing.
      index = reaper.TrackFX_AddByName(target, values.name, false, -1)
    end
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
      local after_failure = project_identity_state(project, path)
      if after_failure.change_count ~= current.change_count then
        local detail = type(failure) == "table" and
                       (failure.message or failure.code or "table error") or
                       tostring(failure)
        fail("transaction_rollback_failed", "The edit failed and no matching undo record was available",
             { plan_id = plan.plan_id, reason = detail,
               undo_description = last_undo, revision = after_failure })
      end
    end
    if type(failure) == "table" and failure.bridge_error then
      fail(failure.code, failure.message, failure.details)
    end
    local detail = type(failure) == "table" and
                   (failure.message or failure.code or "table error") or
                   tostring(failure)
    fail("transaction_apply_failed", detail, { plan_id = plan.plan_id })
  end

  local after = outcome.after
  local after_revision = project_identity_state(project, path)
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
  local current = project_identity_state(project, path)
  if not same_revision_fields(expected, current) then
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
  local after = project_identity_state(project, path)
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

local function unresolved_source_row(track, track_index, track_name, item, take,
                                     source, reason)
  local item_ok, item_guid = false, ""
  if item then item_ok, item_guid = reaper.GetSetMediaItemInfo_String(
    item, "GUID", "", false) end
  local take_ok, take_guid = false, ""
  if take then take_ok, take_guid = reaper.GetSetMediaItemTakeInfo_String(
    take, "GUID", "", false) end
  return {
    track_index = track_index,
    track_guid = track and reaper.GetTrackGUID(track) or "",
    track_name = track_name or "",
    item_guid = item_ok and item_guid or "",
    take_guid = take_ok and take_guid or "",
    source = source and reaper.GetMediaSourceFileName(source) or "",
    source_type = source and reaper.GetMediaSourceType(source) or "",
    reason = reason,
  }
end

-- Reporting only: a capture must never silently render an unresolved take
-- source.  The fixture/project must be opened with media online; this check
-- returns the exact take paths so the caller can repair the project instead of
-- receiving structurally valid digital silence.
function unresolved_sources(project)
  local unresolved = json.array()
  local track_count = reaper.CountTracks(project)
  for track_index = 0, track_count - 1 do
    local track = reaper.GetTrack(project, track_index)
    local track_mute = reaper.GetMediaTrackInfo_Value(track, "B_MUTE") ~= 0
    local name_ok, track_name = reaper.GetSetMediaTrackInfo_String(
      track, "P_NAME", "", false)
    if not name_ok then track_name = "Track " .. (track_index + 1) end
    local item_count = reaper.CountTrackMediaItems(track)
    for item_index = 0, item_count - 1 do
      local item = reaper.GetTrackMediaItem(track, item_index)
      local take = reaper.GetActiveTake(item)
      if take and not reaper.TakeIsMIDI(take) then
        local item_mute = reaper.GetMediaItemInfo_Value(item, "B_MUTE") ~= 0
        local source = reaper.GetMediaItemTake_Source(take)
        local source_length = source and select(1, reaper.GetMediaSourceLength(source)) or 0
        if not (track_mute or item_mute) then
          if not source then
            unresolved[#unresolved + 1] = unresolved_source_row(
              track, track_index, track_name, item, take, nil, "source_missing")
          elseif not finite_number(source_length) or source_length <= 0 then
            unresolved[#unresolved + 1] = unresolved_source_row(
              track, track_index, track_name, item, take, source,
              "source_unresolved")
          end
        end
      end
    end
  end
  return unresolved
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
  local zero = string.pack("<f", 0.0)
  local accessor_start = reaper.GetAudioAccessorStartTime(accessor)
  local accessor_end = reaper.GetAudioAccessorEndTime(accessor)
  local accessor_empty = not finite_number(accessor_start) or
                         not finite_number(accessor_end) or
                         accessor_end <= accessor_start
  local position, zero_blocks = 0, 0
  while position < frames do
    local count = math.min(chunk_frames, frames - position)
    local received = 0
    if not accessor_empty then
      received = reaper.GetAudioAccessorSamples(
        accessor, sample_rate, channels, start_s + position / sample_rate,
        count, buffer)
    end
    if not finite_number(received) then
      file:close()
      error("REAPER returned a non-finite audio accessor sample count at frame " ..
            position)
    end
    if received == -1 then
      file:close()
      error("REAPER rejected an audio accessor sample request at frame " .. position)
    end
    if accessor_empty or received == 0 then
      -- An empty track (or a time range outside the accessor) is intentional
      -- silence, not a failure.  Write an explicit zero block so the file has
      -- the same duration as every other captured stem.
      file:write(string.rep(zero, count * channels))
      zero_blocks = zero_blocks + 1
    else
      -- GetAudioAccessorSamples returns 1 for "audio returned", not the
      -- sample count.  The buffer is already count * channels samples long;
      -- never treat 1 as a one-sample block.
      local packed = {}
      for index = 1, count * channels do
        packed[index] = string.pack("<f", buffer[index] * gain)
      end
      file:write(table.concat(packed))
    end
    position = position + count
  end
  file:close()
  return { bytes = data_bytes + 44, zero_blocks = zero_blocks,
           accessor_start_s = accessor_start, accessor_end_s = accessor_end,
           accessor_empty = accessor_empty, frames = frames }
end

local function project_sample_tracks(arguments)
  local project, path = target_project(arguments)
  local revision = project_identity_state(project, path)
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
  local media_issues = unresolved_sources(project)
  if #media_issues > 0 then
    fail("media_unresolved",
         "Some take sources could not be resolved; accessor audio would be silent",
         { sources = media_issues })
  end
  local ok, result = xpcall(function()
    local files = json.array()
    local track_count = reaper.CountTracks(project)
    for index = 0, track_count - 1 do
      local track = reaper.GetTrack(project, index)
      local accessor = reaper.CreateTrackAudioAccessor(track)
      if not accessor then
        fail("api_unavailable", "Could not create an audio accessor for track " .. index)
      end
      reaper.AudioAccessorUpdate(accessor)
      local name_ok, name = reaper.GetSetMediaTrackInfo_String(
        track, "P_NAME", "", false)
      if not name_ok then name = "Track " .. (index + 1) end
      local safe_name = name:gsub("[^%w%-_ ]", "_")
      if #safe_name > 40 then safe_name = safe_name:sub(1, 40) end
      local file_path = output_dir .. "/rali-" .. job_id .. "-" ..
                        string.format("%02d", index + 1) .. "-" .. safe_name .. ".wav"
      local gain = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
      if not finite_number(gain) then gain = 1 end
      local written = write_accessor_wav(file_path, sample_rate, 2, frame_count,
                                         accessor, start_s, gain)
      reaper.DestroyAudioAccessor(accessor)
      files[#files + 1] = { source_guid = reaper.GetTrackGUID(track),
                            name = name, track_index = index, is_master = false,
                            path = file_path, bytes = written.bytes,
                            zero_blocks = written.zero_blocks,
                            accessor_start_s = written.accessor_start_s,
                            accessor_end_s = written.accessor_end_s,
                            accessor_empty = written.accessor_empty,
                            frames = written.frames }
    end
    return { project_id = revision.project_id, revision = revision,
             files = files, capture_point = "native_audio_accessor_tracks",
             sample_rate = sample_rate, frame_count = frame_count }
  end, function(err) return err end)
  if not ok then
    if type(result) == "table" and result.bridge_error then
      fail(result.code, result.message, result.details)
    end
    fail("capture_failed", "Audio accessor sampling failed: " .. tostring(result))
  end
  result.revision_after = project_identity_state(project, path)
  return result
end

-- Narrow lifecycle surface. It compiles the on-disk script before this loop
-- stops, so a broken file leaves the running bridge untouched instead of
-- killing the only loop; it never saves, closes or reloads a project and it
-- never evaluates arbitrary code: the only thing it loads is this same file.
local pending_reload = nil

local function bridge_lifecycle(arguments)
  local action = arguments.action
  if action ~= "reload" then
    fail("validation_error", "lifecycle action must be 'reload'",
         { action = tostring(action) })
  end
  local expected = arguments.expected_build
  if type(expected) ~= "string" or expected == "" then
    fail("validation_error",
         "expected_build is required so a stale caller cannot reload the bridge")
  end
  if expected ~= bridge_build then
    fail("stale_context", "The caller expected a different running bridge build",
         { expected = expected, running = bridge_build,
           session = bridge_session_id })
  end
  local chunk, compile_error = loadfile(script_path)
  if not chunk then
    fail("reload_rejected",
         "The bridge script on disk does not compile; the running copy was left untouched",
         { script = script_path, error = tostring(compile_error) })
  end
  local file = io.open(script_path, "rb")
  if not file then
    fail("reload_rejected", "The bridge script on disk is unreadable",
         { script = script_path })
  end
  local size = file:seek("end") or 0
  file:close()
  if size < 1024 then
    fail("reload_rejected", "The bridge script on disk is implausibly small",
         { script = script_path, bytes = size })
  end
  pending_reload = { chunk = chunk, script = script_path, bytes = size }
  return { accepted = true, action = "reload", session = bridge_session_id,
           running_build = bridge_build, script = script_path,
           script_bytes = size }
end

local operations = {
  ["bridge.lifecycle"] = bridge_lifecycle,
  health = function()
    local project, path = active_project()
    return { service = "reaper-workbench", protocol = protocol_version,
             bridge_session = bridge_session_id,
             bridge_build = bridge_build,
             script_path = script_path,
             reaper_version = reaper.GetAppVersion(),
             project = project_identity_state(project, path),
             open_projects = projects_list() }
  end,
  ["projects.list"] = projects_list,
  ["project.context"] = project_context,
  ["project.digest"] = project_digest,
  ["project.tempo_at"] = project_tempo_at,
  ["project.render_settings.read"] = project_render_settings_read,
  ["project.capture_render"] = project_capture_render,
  ["project.set_track_mutes"] = project_set_track_mutes,
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
  if _G.reaper_bridge_supersede ~= supersede_token then
    -- A newer boot owns the mailbox; stop without rescheduling.
    return
  end
  if pending_reload then
    local reload = pending_reload
    pending_reload = nil
    reaper.ShowConsoleMsg(string.format(
      "REAPER Workbench: activating %s (%d bytes) over session %s\n",
      reload.script, reload.bytes, bridge_session_id))
    -- The compiled chunk starts the new resident loop and returns; this loop
    -- then ends without rescheduling, so exactly one loop survives. A runtime
    -- failure here leaves no loop at all, which the client reports as a failed
    -- activation instead of silently keeping the old code.
    local reloaded, reload_error = pcall(reload.chunk)
    if not reloaded then
      reaper.ShowConsoleMsg("REAPER Workbench reload failed: " ..
                            tostring(reload_error) .. "\n")
    end
    return
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
