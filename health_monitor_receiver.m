% health_monitor_receiver.m
% Receives HR & SpO2 from ESP32 via UDP and optionally outputs to NI DAQ.
% Run this in MATLAB alongside MonkeyLogic.
%
% UDP packet format from ESP32:  HR=83,SPO2=98,AVG=85,VALID=1
%
% NI DAQ wiring (if using analog output):
%   AO0 -> HR   (0-3.3V = 0-250 bpm)
%   AO1 -> SpO2 (0-3.3V = 0-100%)
%   Connect AO0/AO1 to MonkeyLogic analog input channels AI0/AI1

clear; clc;

UDP_PORT    = 12345;
USE_NI_DAQ  = false;   % Set true to enable NI DAQ analog output
DAQ_DEVICE  = 'Dev1';  % Change to match your NI device name

% ---- Open UDP port ----
u = udpport("LocalPort", UDP_PORT, "Timeout", 1);
fprintf('Listening for ESP32 on UDP port %d ...\n', UDP_PORT);

% ---- NI DAQ setup (optional) ----
if USE_NI_DAQ
    d = daq("ni");
    addoutput(d, DAQ_DEVICE, "ao0", "Voltage");   % HR channel
    addoutput(d, DAQ_DEVICE, "ao1", "Voltage");   % SpO2 channel
    fprintf('NI DAQ %s ready (AO0=HR, AO1=SpO2)\n', DAQ_DEVICE);
end

% ---- Log file ----
log_file = sprintf('health_log_%s.csv', datestr(now, 'yyyymmdd_HHMMSS'));
fid = fopen(log_file, 'w');
fprintf(fid, 'timestamp,hr_bpm,spo2_pct,avg_hr,valid\n');
fprintf('Logging to %s\n\n', log_file);

fprintf('%-12s %-10s %-10s %-10s\n', 'Time', 'HR(bpm)', 'SpO2(%)', 'Status');
fprintf('%s\n', repmat('-', 1, 44));

% ---- Main receive loop ----
% Press Ctrl+C to stop
try
    while true
        if u.NumBytesAvailable > 0
            raw = readline(u);
            raw = strtrim(char(raw));

            % Parse:  HR=83,SPO2=98,AVG=85,VALID=1
            hr   = parse_field(raw, 'HR');
            spo2 = parse_field(raw, 'SPO2');
            avg  = parse_field(raw, 'AVG');
            vld  = parse_field(raw, 'VALID');

            t_str = datestr(now, 'HH:MM:SS');

            if vld == 1
                status = 'OK';
            elseif hr == -1
                status = 'No finger';
            else
                status = 'Settling';
            end

            fprintf('%-12s %-10d %-10d %s\n', t_str, hr, spo2, status);
            fprintf(fid, '%s,%d,%d,%d,%d\n', t_str, hr, spo2, avg, vld);

            % NI DAQ analog output
            if USE_NI_DAQ && vld == 1
                hr_v   = max(0, min(3.3, hr   / 250 * 3.3));
                spo2_v = max(0, min(3.3, spo2 / 100 * 3.3));
                write(d, [hr_v, spo2_v]);
            end
        end
        pause(0.05);
    end
catch ME
    if ~strcmp(ME.identifier, 'MATLAB:class:InvalidHandle')
        fprintf('\nStopped: %s\n', ME.message);
    end
end

% ---- Cleanup ----
fclose(fid);
clear u;
if USE_NI_DAQ && exist('d', 'var')
    release(d);
end
fprintf('\nLog saved to %s\n', log_file);

% ----------------------------------------------------------------
function val = parse_field(str, field)
    tok = regexp(str, [field '=(-?\d+)'], 'tokens');
    if isempty(tok)
        val = -1;
    else
        val = str2double(tok{1}{1});
    end
end
