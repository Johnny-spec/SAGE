import re
import os
import json
from collections import defaultdict

log_path = r'C:\Users\wshao\Downloads\AuthCapture_GD-ZS-M00631_2129\AuthCapture_GD-ZS-M00631_2129\netlogon.log'
out_dir = r'C:\workfile\SkillsCollection\Temp\authcapture_analysis\netlogon'
os.makedirs(out_dir, exist_ok=True)

with open(log_path, 'r', encoding='latin-1') as f:
    lines = f.readlines()

# Match format: 08/14 10:33:48 [MISC] [20364] DsGetDcName function called: client PID=15824, Dom:PAICDOM.LOCAL Acct:(null) Flags: FORCE LDAPONLY 
# Match return format: 08/14 10:33:53 [MISC] [20364] DsGetDcName function returns 1355 (client PID=15824): Dom:PAICDOM.LOCAL Acct:(null) Flags: FORCE LDAPONLY 

calls = []
current_calls_by_thread = {}  # thread -> call dict

for idx, line in enumerate(lines):
    line = line.strip()
    if not line:
        continue
    
    # regex to parse a line
    # 08/14 10:33:48 [MISC] [20364] text
    m = re.match(r'^(\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+\[([^\]]+)\]\s+\[(\d+)\]\s+(.*)$', line)
    if not m:
        continue
    
    timestamp, log_type, thread_id, content = m.groups()
    thread_id = int(thread_id)
    
    # Check if this is DsGetDcName function called
    called_m = re.search(r'DsGetDcName function called: client PID=(\d+),\s+Dom:([^\s]+)\s+Acct:([^\s]+)\s+Flags:\s*(.*)$', content)
    if called_m:
        client_pid = int(called_m.group(1))
        domain = called_m.group(2)
        acct = called_m.group(3)
        flags = called_m.group(4).strip()
        
        call_obj = {
            'start_time': timestamp,
            'thread': thread_id,
            'client_pid': client_pid,
            'domain': domain,
            'flags': flags,
            'lines': [line],
            'udp_pings': [],
            'retries_count': 0,
            'intermediate_returns_121': 0,
            'received_lines': [],
            'end_time': None,
            'return_code': None,
            'dc_name': None,
            'dc_address': None,
            'results': None
        }
        current_calls_by_thread[thread_id] = call_obj
        calls.append(call_obj)
        continue
        
    # Check if there is an active call in this thread
    if thread_id in current_calls_by_thread:
        call_obj = current_calls_by_thread[thread_id]
        call_obj['lines'].append(line)
        
        # Check if UDP ping (NetpDcPingListIp)
        # NetpDcPingListIp: PAICDOM.LOCAL: Sent UDP ping to 30.162.29.75
        ping_m = re.search(r'NetpDcPingListIp:.*Sent UDP ping to ([\d\.]+)', content)
        if ping_m:
            ip = ping_m.group(1)
            call_obj['udp_pings'].append((timestamp, ip))
            
        # Check if second round of pings (retries)
        # In netlogon logs, 121 means a timeout, and then it may retry. Let's count "NetpDcGetName: NetpDcGetNameIp for PAICDOM.LOCAL returned 121"
        if "returned 121" in content:
            call_obj['intermediate_returns_121'] += 1
            # Note that a retry might follow after a return 121. The log matches:
            # NetpDcGetName: NetpDcGetNameIp for PAICDOM.LOCAL returned 121.
            # Usually after that, it retries or has second rounds. Let's count them.
            
        # Check received ping responses or other response lines:
        if "received" in content.lower() or "respond" in content.lower() or "ping reply" in content.lower() or "reply" in content.lower():
            call_obj['received_lines'].append(line)
            
        # Check if DsGetDcName function returns
        # DsGetDcName function returns 1355 (client PID=15824): Dom:PAICDOM.LOCAL Acct:(null) Flags: FORCE LDAPONLY 
        return_m = re.search(r'DsGetDcName function returns (\d+) \(client PID=\d+\):', content)
        if return_m:
            call_obj['end_time'] = timestamp
            call_obj['return_code'] = int(return_m.group(1))
            current_calls_by_thread.pop(thread_id, None)
            continue
            
        # Check if DsGetDcName: results as follows: DCName:\\SDBDCBS04.sdb.local DCAddress:\\22.8.32.21 ...
        results_m = re.search(r'DsGetDcName: results as follows:\s+DCName:([^\s]+)\s+DCAddress:([^\s]+)', content)
        if results_m:
            call_obj['dc_name'] = results_m.group(1)
            call_obj['dc_address'] = results_m.group(2)
            call_obj['results'] = content

# Perform counts and detailed analysis
# Group calls by domain domain
# PAICDOM.LOCAL, sdb.local, (null)

stats = defaultdict(lambda: {
    'count': 0,
    'return_codes': defaultdict(int),
    'pids': defaultdict(int),
    'time_start': None,
    'time_end': None,
    'total_pings': 0,
    'unique_pings_by_ip': defaultdict(int),
    'retries_count': 0,
    'successes': 0,
    'failures': 0,
    'returned_121_count': 0
})

for c in calls:
    dom = c['domain']
    # Normalize domain to PAICDOM.LOCAL, sdb.local or (null)
    dom_key = dom
    if dom_key == '(null)':
        dom_key = 'null'
        
    s = stats[dom_key]
    s['count'] += 1
    
    if c['return_code'] is not None:
        rcode = c['return_code']
        s['return_codes'][rcode] += 1
        if rcode == 0:
            s['successes'] += 1
        else:
            s['failures'] += 1
    else:
        s['return_codes']['UNFINISHED'] += 1
        s['failures'] += 1
        
    s['pids'][c['client_pid']] += 1
    
    if s['time_start'] is None or c['start_time'] < s['time_start']:
        s['time_start'] = c['start_time']
    if c['end_time']:
        if s['time_end'] is None or c['end_time'] > s['time_end']:
            s['time_end'] = c['end_time']
            
    # UDP pings
    s['total_pings'] += len(c['udp_pings'])
    for ts, ip in c['udp_pings']:
        s['unique_pings_by_ip'][ip] += 1
        
    s['returned_121_count'] += c['intermediate_returns_121']

# Let's count second-round retries.
# In Netlogon, a "second-round" of pinging happens if NetpDcGetNameIp returned 121 (timeout) and then we send pings again.
# Let's check how many times we see a ping being sent to the same IP. Or let's see how many pings occurred after the first returned 121 in each call.
for c in calls:
    if c['intermediate_returns_121'] > 0:
        # Pings sent after the first 121 are retries
        first_121_idx = -1
        for i, line in enumerate(c['lines']):
            if "returned 121" in line:
                first_121_idx = i
                break
        if first_121_idx != -1:
            retries = 0
            for line in c['lines'][first_121_idx:]:
                if "Sent UDP ping" in line:
                    retries += 1
            c['retries_count'] = retries
            
            # Add to domain stats
            dom_key = c['domain'] if c['domain'] != '(null)' else 'null'
            stats[dom_key]['retries_count'] += retries

# Write down the output report
report_path = os.path.join(out_dir, "analysis_report.txt")
with open(report_path, 'w', encoding='utf-8') as rf:
    rf.write("============================================================\n")
    rf.write("      DSGETDCNAME NETLOGON CALL RECONSTRUCTION REPORT\n")
    rf.write("============================================================\n\n")
    
    rf.write("-------------------- RECONSTRUCTED CALLS --------------------\n")
    for i, c in enumerate(calls):
        rf.write(f"Call #{i+1}:\n")
        rf.write(f"  Timestamp (Start): {c['start_time']}\n")
        rf.write(f"  Timestamp (End):   {c['end_time']}\n")
        rf.write(f"  Thread ID:         {c['thread']}\n")
        rf.write(f"  Client PID:        {c['client_pid']}\n")
        rf.write(f"  Domain Requested:  {c['domain']}\n")
        rf.write(f"  Flags:             {c['flags']}\n")
        rf.write(f"  Return Code:       {c['return_code']}\n")
        if c['dc_name']:
            rf.write(f"  DC Name Found:     {c['dc_name']}\n")
            rf.write(f"  DC Address Found:  {c['dc_address']}\n")
        rf.write(f"  Total UDP Pings:   {len(c['udp_pings'])}\n")
        if c['udp_pings']:
            # count of pings by unique IP in this call
            pip_counts = defaultdict(int)
            for ts, ip in c['udp_pings']:
                pip_counts[ip] += 1
            rf.write("  UDP Pings per IP:  " + ", ".join(f"{ip}({cnt})" for ip, cnt in pip_counts.items()) + "\n")
        rf.write(f"  Second-round Retries Count: {c['retries_count']}\n")
        rf.write(f"  NetpDcGetNameIp returned 121 Count: {c['intermediate_returns_121']}\n")
        rf.write(f"  Received/Response Lines: {len(c['received_lines'])}\n")
        if c['received_lines']:
            for rl in c['received_lines']:
                rf.write(f"    - {rl}\n")
        rf.write("  Full Log Segment for this context:\n")
        for line in c['lines']:
            rf.write(f"    {line}\n")
        rf.write("-" * 60 + "\n\n")

    rf.write("-------------------- GROUPED STATISTICS --------------------\n")
    for dom, s in stats.items():
        rf.write(f"Domain: {dom}\n")
        rf.write(f"  Total Call Count:                  {s['count']}\n")
        rf.write(f"  Time Range:                         {s['time_start']} to {s['time_end']}\n")
        rf.write(f"  Result Code Distribution:          {dict(s['return_codes'])}\n")
        rf.write(f"  PID Distribution:                  {dict(s['pids'])}\n")
        rf.write(f"  Total UDP Pings Sent:              {s['total_pings']}\n")
        if s['unique_pings_by_ip']:
            rf.write("  UDP Pings sent to unique IPs:\n")
            # Sort IPs
            for ip, cnt in sorted(s['unique_pings_by_ip'].items(), key=lambda x: x[1], reverse=True):
                rf.write(f"    - {ip}: {cnt} pings\n")
        rf.write(f"  Total Second-round Retries:        {s['retries_count']}\n")
        rf.write(f"  Total returned 121 (Timeout):      {s['returned_121_count']}\n")
        rf.write(f"  Total Successes:                   {s['successes']}\n")
        rf.write(f"  Total Failures:                    {s['failures']}\n")
        rf.write("\n" + "="*40 + "\n")

print("Report generated successfully.")
