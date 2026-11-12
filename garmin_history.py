"""Garmin history for the IMLP dashboard (script version 5).
need: what was found, and which sessions still need their FIT file (every new workout to review, plus rides and runs without FIT readings).
build: writes ./history.json (sessions, days, reports) and prints the summary for the report, with the new workouts to review.
publish <report.json>: checks today's report (and its workout reviews) and adds it to ./history.json."""
import sys, os, io, re, json, glob, math, zipfile, statistics as st, datetime as dt

ARGS = sys.argv[1:]
def arg(name, default):
    return type(default)(ARGS[ARGS.index(name) + 1]) if name in ARGS else default
DIR = arg('--dir', '/tmp/claude-tool-results')
T = {'ftp': arg('--ftp', 267.0), 'lthr': arg('--lthr', 172.0), 'css_s': arg('--css', 100.0),
     'hr_rest': arg('--hrrest', 48.0), 'hr_max': arg('--hrmax', 194.0), 'cap': 152, 'z2_low': 142, 'mod_pct': 80.0}
SCRIPT_VERSION = 5
REVIEW_FROM = arg('--review-from', '2026-11-11')  # workout reviews start with the sessions of Wed Nov 11 (CD's request)
REVIEW_DAYS, REVIEW_MAX, REVIEW_MIN_S = 7, 5, 600   # only the last 7 days, at most 5 a morning, sessions of 10 min or more
SPORT = {'lap_swimming': 'swim', 'open_water_swimming': 'swim', 'indoor_cycling': 'bike', 'road_biking': 'bike',
         'cycling': 'bike', 'virtual_ride': 'bike', 'gravel_cycling': 'bike', 'mountain_biking': 'bike',
         'running': 'run', 'treadmill_running': 'run', 'trail_running': 'run', 'track_running': 'run',
         'strength_training': 'strength', 'walking': 'walk', 'hiking': 'walk', 'golf': 'golf',
         'yoga': 'mobility', 'pilates': 'mobility', 'flexibility_training': 'mobility', 'breathwork': 'mobility'}
INDOOR = {'indoor_cycling', 'treadmill_running', 'virtual_ride', 'lap_swimming'}
SKIP_IDS = {'24839293887'}  # Jones Beach bike leg, recorded twice; 24848785570 is the copy with power
RACE_IDS = {'24839293881', '24848785570', '24839293895'}
FIT_KEYS = ['fit', 'method', 'main_s', 'main_hr', 'main_w', 'main_np', 'main_mps', 'main_cad', 'ef', 'dec',
            'over_cap_s', 'z2_s', 'under_z2_s']


# ---------- reading saved tool results ----------
def files():
    out = []
    for p in glob.glob(os.path.join(DIR, '**', '*'), recursive=True):
        if os.path.isfile(p):
            out.append(p)
    return sorted(out, key=os.path.getmtime)

def parse_text(p):
    raw = open(p, 'rb').read()
    if raw[:2] == b'PK':
        return 'zip', raw
    try:
        txt = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None, None
    try:
        x = json.loads(txt)
    except ValueError:
        x = None
    if isinstance(x, list) and x and isinstance(x[0], dict) and x[0].get('type') == 'text':  # saved content blocks
        txt = ''.join(b.get('text', '') for b in x if isinstance(b, dict) and b.get('type') == 'text')
        x = None
    if '<untrusted-document-' in txt:  # read_asset result: the file's JSON sits between the wrapper tags
        a = txt.index('>', txt.index('<untrusted-document-')) + 1
        b = txt.index('</untrusted-document-', a)
        txt, x = txt[a:b], None
    if x is None:
        try:
            x = json.loads(txt)
        except ValueError:
            return None, None
    return 'json', x


def classify():
    found = {'old': None, 'plan': None, 'reports_list': None, 'acts': [], 'daily': [], 'hrv': [], 'sleep': [], 'fit': {}}
    for p in files():
        kind, x = parse_text(p)
        if kind == 'zip':
            try:
                z = zipfile.ZipFile(io.BytesIO(x))
                n = [m for m in z.namelist() if m.lower().endswith('.fit')]
                mid = re.match(r'(\d+)_', os.path.basename(n[0])) if n else None
                if mid:
                    found['fit'][mid.group(1)] = z.read(n[0])
            except zipfile.BadZipFile:
                pass
        elif kind == 'json':
            if isinstance(x, dict) and 'acts' in x and 'days' in x:
                found['old'] = x
            elif isinstance(x, dict) and 'sessions' in x and 'weeks' in x and 'targets' in x:
                found['plan'] = x
            elif isinstance(x, list) and x and isinstance(x[0], dict) and 'headline' in x[0] and 'items' in x[0]:
                found['reports_list'] = x
            elif isinstance(x, list) and x and isinstance(x[0], dict) and 'activityId' in x[0]:
                found['acts'].append(x)
            elif isinstance(x, dict) and isinstance(x.get('data'), list) and x['data'] and 'calendarDate' in x['data'][0]:
                found['daily'].append(x['data'])
            elif isinstance(x, dict) and 'hrvSummaries' in x:
                found['hrv'].append(x['hrvSummaries'])
            elif isinstance(x, list) and x and isinstance(x[0], dict) and 'sleepTimeSeconds' in x[0]:
                found['sleep'].append(x)
    return found


# ---------- FIT metrics (rides and runs) ----------
YDMIN = 65.6168

def fit_read(data):
    import fitdecode
    recs, laps, steps, sess, lengths, strap = [], [], {}, {}, [], False
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED) as fr:
        for f in fr:
            if not isinstance(f, fitdecode.FitDataMessage):
                continue
            g = lambda k: f.get_value(k) if f.has_field(k) else None
            if f.name == 'record' and g('timestamp') is not None:
                spd, cad, fc = g('enhanced_speed'), g('cadence'), g('fractional_cadence')
                if cad is not None and fc:
                    cad += fc
                recs.append((g('timestamp').timestamp(), g('power'), g('heart_rate'), cad,
                             spd if spd is not None else g('speed'), g('distance')))
            elif f.name == 'lap':
                s0 = g('start_time')
                rc = g('avg_running_cadence')
                sp = g('enhanced_avg_speed')
                laps.append({'start': s0.timestamp() if s0 else None, 'timer': g('total_timer_time') or 0,
                             'elapsed': g('total_elapsed_time') or g('total_timer_time') or 0,
                             'intensity': g('intensity'), 'step': g('wkt_step_index'), 'dist': g('total_distance'),
                             'hr': g('avg_heart_rate'), 'hr_max': g('max_heart_rate'), 'w': g('avg_power'), 'np': g('normalized_power'),
                             'cad': rc if rc is not None else g('avg_cadence'), 'spd': sp if sp is not None else g('avg_speed'),
                             'stroke': g('swim_stroke')})
            elif f.name == 'workout_step':
                steps[g('message_index')] = {'intensity': g('intensity'), 'name': g('wkt_step_name'), 'type': g('target_type'),
                                             'lo': g('custom_target_value_low'), 'hi': g('custom_target_value_high')}
            elif f.name == 'length':
                s0 = g('start_time')
                lengths.append({'start': s0.timestamp() if s0 else None, 'sec': g('total_timer_time') or 0,
                                'strokes': g('total_strokes'), 'stroke': g('swim_stroke'), 'active': str(g('length_type')) == 'active'})
            elif f.name == 'device_info':
                if 'heart_rate' in (str(g('antplus_device_type')), str(g('device_type'))):
                    strap = True
            elif f.name == 'session':
                sess = {'sport': str(g('sport')), 'dist': g('total_distance'), 'pool': g('pool_length')}
    recs.sort(key=lambda r: r[0])
    sess['rec_dist'] = next((r[5] for r in reversed(recs) if r[5]), None)
    sess['lengths'], sess['strap'] = lengths, strap
    return [r[:5] for r in recs], laps, steps, sess

def resample(recs, max_gap=10):
    out = []
    for r in recs:
        t = int(round(r[0]))
        if out and t <= out[-1][0]:
            continue
        if out and 1 < t - out[-1][0] <= max_gap:
            prev = out[-1]
            out.extend((prev[0] + k,) + prev[1:] for k in range(1, t - prev[0]))
        out.append((t,) + tuple(r[1:]))
    return out

def np_power(ps):
    if len(ps) < 30:
        return None
    roll, s = [], 0.0
    for i, p in enumerate(ps):
        s += p
        if i >= 30:
            s -= ps[i - 30]
        if i >= 29:
            roll.append(s / 30)
    return (sum(x ** 4 for x in roll) / len(roll)) ** 0.25

def block(rows, run):
    hr = [r[2] for r in rows if r[2]]
    p = [r[1] if r[1] is not None else 0 for r in rows]
    cad = [r[3] for r in rows if r[3]]
    spd = [r[4] for r in rows if r[4] is not None]
    has_p = bool(rows) and sum(1 for r in rows if r[1]) > 0.5 * len(rows)
    npw = np_power(p) if has_p else None
    c = st.mean(cad) if cad else None
    return {'s': len(rows), 'hr': round(st.mean(hr), 1) if hr else None, 'w': round(st.mean(p), 1) if has_p else None,
            'np': round(npw, 1) if npw else None, 'cad': round(c * 2 if run else c, 1) if c else None,
            'mps': round(st.mean(spd), 3) if spd else None}

def eff(b, run):
    if not b['hr']:
        return None
    v = b['mps'] if run else (b['np'] or b['w'])
    return v / b['hr'] if v else None

def fit_metrics(data):
    recs, laps, steps, sess = fit_read(data)
    if not recs:
        return None
    run = sess.get('sport') == 'running'
    out = {}
    if run and sess.get('dist') and sess.get('rec_dist'):
        k = sess['dist'] / sess['rec_dist']
        if abs(k - 1) > 0.02:  # treadmill: scale speeds to the distance saved on the watch
            recs = [r[:4] + ((r[4] * k) if r[4] is not None else None,) for r in recs]
    sec = resample(recs)
    for l in laps:
        k = l['intensity'] if l['intensity'] is not None else (steps.get(l['step']) or {}).get('intensity') if l['step'] is not None else None
        l['kind'] = str(k) if k is not None else None
    laps = sorted([l for l in laps if l['start'] is not None], key=lambda l: l['start'])
    blocks, cur = [], []
    for l in laps:
        if l['kind'] == 'active':
            cur.append(l)
        elif cur:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    if any(l['kind'] for l in laps) and blocks:
        best = max(blocks, key=lambda b: sum(l['elapsed'] for l in b))
        a, z = best[0]['start'], best[-1]['start'] + best[-1]['elapsed']
        rows = [r for r in sec if a <= r[0] < z]
        out['method'] = 'workout block'
    else:
        t0, t1 = sec[0][0], sec[-1][0]
        a, z = (300, 120) if run else (600, 300)
        rows = [r for r in sec if t0 + a <= r[0] < t1 - z]
        out['method'] = 'trimmed window'
        if run and len(rows) > 600:
            sp = [r[4] for r in rows if r[4]]
            med = st.median(sp) if sp else None
            if med:
                for i in range(max(len(rows) // 2, 10), len(rows)):
                    if sum((r[4] or 0) for r in rows[i - 10:i]) / 10 > 1.3 * med:
                        rows = rows[:max(i - 40, 0)]
                        break
    m = block(rows, run)
    hrs = [r[2] for r in rows if r[2]]
    e = eff(m, run)
    out.update({'fit': 1, 'main_s': m['s'], 'main_hr': m['hr'], 'main_w': m['w'], 'main_np': m['np'],
                'main_mps': m['mps'], 'main_cad': m['cad'],
                'ef': (round(e * YDMIN, 3) if run else round(e, 3)) if e else None, 'dec': None,
                'over_cap_s': sum(1 for x in hrs if x > T['cap']),
                'z2_s': sum(1 for x in hrs if T['z2_low'] <= x <= T['cap']),
                'under_z2_s': sum(1 for x in hrs if x < T['z2_low'])})
    if len(rows) >= 1200:
        h = len(rows) // 2
        e1, e2 = eff(block(rows[:h], run), run), eff(block(rows[h:], run), run)
        if e1 and e2:
            out['dec'] = round((e1 - e2) / e1 * 100, 1)
    return out


# ---------- rows ----------
def rnd(v, n=0):
    return None if v is None else (round(v, n) if n else int(round(v)))

def act_row(a):
    k = a['activityType']['typeKey']
    sport = SPORT.get(k, 'other')
    dist = a.get('distance') or 0
    cad = a.get('averageBikingCadenceInRevPerMinute') if sport == 'bike' else \
        a.get('averageRunningCadenceInStepsPerMinute') if sport == 'run' else None
    r = {'id': str(a['activityId']), 'date': a['startTimeLocal'][:10], 'start': a['startTimeLocal'][11:16],
         'sport': sport, 'type': k, 'name': a.get('activityName') or k, 'indoor': 1 if k in INDOOR else 0,
         'race': 1 if str(a['activityId']) in RACE_IDS or (a.get('eventType') or {}).get('typeKey') == 'race' else 0,
         'dur_s': rnd(a.get('duration')), 'mov_s': rnd(a.get('movingDuration') or a.get('duration')),
         'dist_m': rnd(dist), 'hr': rnd(a.get('averageHR')), 'hr_max': rnd(a.get('maxHR')),
         'w': rnd(a.get('avgPower')), 'np': rnd(a.get('normPower')), 'cad': rnd(cad),
         'mps': rnd(a.get('averageSpeed'), 3), 'yd': rnd(dist / 0.9144) if sport == 'swim' and dist else None}
    r.update({k2: None for k2 in FIT_KEYS})
    r['fit'] = 0
    return r

def wall(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M') if ms else None

DAY_KEYS = ['sleep_s', 'sleep_start', 'sleep_end', 'sleep_score', 'deep_s', 'rem_s', 'awake_s', 'hrv', 'hrv_week',
            'hrv_status', 'hrv_low', 'hrv_high', 'rhr', 'bb_max', 'bb_min', 'stress', 'steps']

def merge(found):
    old = found['old'] or {'acts': [], 'days': []}
    acts = {r['id']: r for r in old['acts']}
    for lst in found['acts']:
        for a in lst:
            aid = str(a['activityId'])
            k = a['activityType']['typeKey']
            if aid in SKIP_IDS or a.get('isParent') or k in ('multi_sport', 'transition_v2'):
                continue
            new = act_row(a)
            if aid in acts:
                for k2 in FIT_KEYS:
                    new[k2] = acts[aid].get(k2)
            acts[aid] = new
    days = {r['date']: dict(r) for r in old['days']}
    def day(d):
        return days.setdefault(d, dict({'date': d}, **{k: None for k in DAY_KEYS}))
    for lst in found['sleep']:
        for s in lst:
            if not s.get('sleepTimeSeconds'):
                continue
            sc = ((s.get('sleepScores') or {}).get('overall') or {}).get('value')
            day(s['calendarDate']).update({'sleep_s': s['sleepTimeSeconds'], 'sleep_start': wall(s.get('sleepStartTimestampLocal')),
                                           'sleep_end': wall(s.get('sleepEndTimestampLocal')), 'sleep_score': sc,
                                           'deep_s': s.get('deepSleepSeconds'), 'rem_s': s.get('remSleepSeconds'),
                                           'awake_s': s.get('awakeSleepSeconds')})
    for lst in found['hrv']:
        for h in lst:
            if h['calendarDate'] < '2026-07-01':
                continue
            b = h.get('baseline') or {}
            day(h['calendarDate']).update({'hrv': h.get('lastNightAvg'), 'hrv_week': h.get('weeklyAvg'),
                                           'hrv_status': h.get('status'), 'hrv_low': b.get('balancedLow'),
                                           'hrv_high': b.get('balancedUpper')})
    for lst in found['daily']:
        for d in lst:
            if d['calendarDate'] < '2026-07-01':
                continue
            day(d['calendarDate']).update({'rhr': (d.get('heartRate') or {}).get('restingValue'),
                                           'bb_max': (d.get('bodyBattery') or {}).get('maxValue'),
                                           'bb_min': (d.get('bodyBattery') or {}).get('minValue'),
                                           'stress': (d.get('stress') or {}).get('avgLevel'),
                                           'steps': ((d.get('movement') or {}).get('steps') or {}).get('value')})
    return acts, days

def needs_fit(r, since):
    return (not r.get('fit') and not r.get('race') and r['date'] >= since and
            ((r['sport'] == 'bike' and (r['dur_s'] or 0) >= 1800) or (r['sport'] == 'run' and (r['dur_s'] or 0) >= 1200)))


# ---------- load (same rules as the dashboard) ----------
def trimp(hr):
    x = min(max((hr - T['hr_rest']) / (T['hr_max'] - T['hr_rest']), 0), 1)
    return x * 0.64 * math.exp(1.92 * x)

def tss(r):
    h = (r['dur_s'] or 0) / 3600
    if r['sport'] == 'bike' and r.get('np'):
        return h * (r['np'] / T['ftp']) ** 2 * 100
    if r['sport'] == 'swim' and r.get('dist_m') and r.get('mov_s'):
        return r['mov_s'] / 3600 * ((r['dist_m'] / r['mov_s']) / (91.44 / T['css_s'])) ** 3 * 100
    if r['sport'] in ('run', 'bike', 'other') and r.get('hr'):
        return h * 100 * trimp(r['hr']) / trimp(T['lthr'])
    return 0.0

def summary(acts, days, today, plan=None):
    al = sorted(acts.values(), key=lambda r: (r['date'], r['start']))
    d0 = dt.date.fromisoformat(al[0]['date'])
    jr = lambda x, n=0: math.floor(x * 10 ** n + 0.5) / 10 ** n  # JavaScript's Math.round, as the dashboard rounds
    load = {}
    for r in al:
        load[r['date']] = load.get(r['date'], 0) + jr(tss(r))
    ctl = atl = 0.0
    pmc = {}
    d = d0
    while d.isoformat() <= today:
        k = d.isoformat()
        form = ctl - atl
        ctl += (load.get(k, 0) - ctl) / 42
        atl += (load.get(k, 0) - atl) / 7
        pmc[k] = (jr(ctl, 1), jr(atl, 1), jr(form, 1))
        d += dt.timedelta(1)
    wk = (dt.date.fromisoformat(today) - dt.timedelta(7)).isoformat()
    recent = [r for r in al if r['date'] >= (dt.date.fromisoformat(today) - dt.timedelta(3)).isoformat()]
    def ef_vs(sport):  # the dashboard's Latest sessions comparison: latest ride/run vs the six before it, same kind
        lst = [r for r in al if r['sport'] == sport and not r.get('race') and (r['dur_s'] or 0) >= 600]
        if not lst:
            return None
        a = lst[-1]
        lo_s, lo_hr = (1200, 120) if sport == 'bike' else (900, 125)
        like = [r['ef'] for r in lst[:-1] if r['indoor'] == a['indoor'] and r.get('ef') and
                (r.get('main_s') or 0) >= lo_s and (r.get('main_hr') or 0) >= lo_hr][-6:]
        med = st.median(like) if like else None
        return {'id': a['id'], 'date': a['date'], 'name': a['name'], 'indoor': a['indoor'], 'ef': a.get('ef'),
                'prev6_median': jr(med, 3) if med else None,
                'delta_pct': jr((a['ef'] - med) / med * 100, 1) if med and a.get('ef') else None}
    dl = [days[k] for k in sorted(days)]
    last7 = [x for x in dl if x['date'] > wk and x.get('sleep_s')]
    ready = readiness(dl)
    return {
        'today': today, 'ctl_atl_form': pmc.get(today), 'ctl_7d_ago': (pmc.get(wk) or (None,))[0],
        'tss_last_7d': round(sum(v for k, v in load.items() if wk < k <= today)),
        'recent_sessions': [{k: r.get(k) for k in ('id', 'date', 'start', 'name', 'type', 'dur_s', 'hr', 'w', 'np', 'cad',
                                                     'main_s', 'main_hr', 'main_w', 'main_np', 'main_mps', 'main_cad', 'ef',
                                                     'dec', 'over_cap_s')} | {'tss': int(jr(tss(r)))} for r in recent],
        'ef_vs_prev6': {'ride': ef_vs('bike'), 'run': ef_vs('run')},
        'sleep_7n_avg_h': round(st.mean(x['sleep_s'] for x in last7) / 3600, 2) if last7 else None,
        'readiness': ready[-4:],
        'last_days': [{k: x.get(k) for k in ('date', 'sleep_s', 'sleep_start', 'sleep_end', 'sleep_score', 'hrv', 'hrv_week',
                                             'hrv_status', 'hrv_low', 'hrv_high', 'rhr', 'bb_max')} for x in dl[-8:]],
        'charts': chart_stats(al, dl, pmc, load, today),
        'plan': plan_summary(plan, al, pmc, load, today),
        'targets_used': {k: T[k] for k in ('ftp', 'lthr', 'css_s', 'hr_rest', 'hr_max', 'cap', 'z2_low', 'mod_pct')},
    }


def chart_stats(al, dl, pmc, load, today):
    """Numbers behind each dashboard chart, for the coach's read under it."""
    td = dt.date.fromisoformat(today)
    ago = lambda n: (td - dt.timedelta(n)).isoformat()
    jr = lambda x, n=0: None if x is None else math.floor(x * 10 ** n + 0.5) / 10 ** n
    p = lambda k: pmc.get(k) or (None, None, None)
    keys = sorted(pmc)
    last28 = [k for k in keys if ago(28) < k <= today]
    peak = max(keys, key=lambda k: pmc[k][0]) if keys else None
    low = min(last28, key=lambda k: pmc[k][2]) if last28 else None
    tss_in = lambda a, b: int(sum(v for k, v in load.items() if a < k <= b))
    pace = lambda mps: '%d:%02d' % divmod(int(round(1609.344 / mps)), 60) if mps else None
    def eff(sport, n=8):
        lo_s, lo_hr = (1200, 120) if sport == 'bike' else (900, 125)
        lst = [r for r in al if r['sport'] == sport and not r.get('race') and r.get('ef') and (r.get('main_s') or 0) >= lo_s and (r.get('main_hr') or 0) >= lo_hr]
        return [{'date': r['date'], 'name': r['name'], 'indoor': r['indoor'], 'ef': r['ef'], 'main_min': round((r.get('main_s') or 0) / 60),
                 'main_hr': jr(r.get('main_hr')), 'main_np': jr(r.get('main_np')) if sport == 'bike' else None,
                 'pace_mi': pace(r.get('main_mps')) if sport == 'run' else None} for r in lst[-n:]]
    dec = [{'date': r['date'], 'sport': r['sport'], 'name': r['name'], 'dec': r['dec'], 'main_min': round((r.get('main_s') or 0) / 60)}
           for r in al if r.get('dec') is not None and not r.get('race')][-8:]
    def clock(m):
        m = int(round(m)) % 1440
        h, mm = divmod(m, 60)
        return '%d:%02d %s' % ((h - 1) % 12 + 1, mm, 'AM' if h < 12 else 'PM')
    def mins(ts, night):
        if not ts:
            return None
        m = int(ts[11:13]) * 60 + int(ts[14:16])
        return m + 1440 if night and m < 720 else m
    n28 = [x for x in dl if ago(28) < x['date'] <= today and x.get('sleep_s')]
    n7 = [x for x in n28 if x['date'] > ago(7)]
    on = [mins(x.get('sleep_start'), True) for x in n28 if x.get('sleep_start')]
    wk = [mins(x.get('sleep_end'), False) for x in n28 if x.get('sleep_end')]
    sleep = {'nights': len(n28), 'avg_h': jr(st.mean(x['sleep_s'] for x in n28) / 3600, 2) if n28 else None,
             'avg_7_h': jr(st.mean(x['sleep_s'] for x in n7) / 3600, 2) if n7 else None,
             'nights_8h': sum(1 for x in n28 if x['sleep_s'] >= 8 * 3600), 'nights_under_6_5h': sum(1 for x in n28 if x['sleep_s'] < 6.5 * 3600),
             'avg_sleep_start': clock(st.mean(on)) if on else None, 'avg_wake': clock(st.mean(wk)) if wk else None,
             'starts_after_10pm': sum(1 for m in on if m > 22 * 60), 'latest_start': clock(max(on)) if on else None, 'earliest_start': clock(min(on)) if on else None}
    h60 = [x for x in dl if ago(60) < x['date'] <= today and x.get('hrv') is not None]
    h14 = [x for x in h60 if x['date'] > ago(14)]
    last = h60[-1] if h60 else {}
    then = [x for x in h60 if x['date'] <= ago(28)]
    hrv = {'last_night': last.get('hrv'), 'week_avg': last.get('hrv_week'), 'baseline': [last.get('hrv_low'), last.get('hrv_high')], 'status': last.get('hrv_status'),
           'week_avg_28d_ago': then[-1].get('hrv_week') if then else None,
           'nights_under_baseline_14d': sum(1 for x in h14 if x.get('hrv_low') and x['hrv'] < x['hrv_low']),
           'status_days_14d': {s: sum(1 for x in h14 if x.get('hrv_status') == s) for s in ('BALANCED', 'UNBALANCED', 'LOW', 'POOR')}}
    r60 = [x for x in dl if ago(60) < x['date'] <= today and x.get('rhr') is not None]
    r7 = [x['rhr'] for x in r60 if x['date'] > ago(7)]
    r7then = [x['rhr'] for x in r60 if ago(35) < x['date'] <= ago(28)]
    rd = readiness(dl)
    rhr = {'today': r60[-1]['rhr'] if r60 else None, 'baseline': rd[-1]['rhr_base'] if rd else None,
           'avg_7d': jr(st.mean(r7), 1) if r7 else None, 'avg_7d_4wk_ago': jr(st.mean(r7then), 1) if r7then else None,
           'min_60d': min(x['rhr'] for x in r60) if r60 else None, 'max_60d': max(x['rhr'] for x in r60) if r60 else None}
    w0 = dt.date(2026, 10, 26)
    mon = td - dt.timedelta(td.weekday())
    weeks = []
    for i in range(5, -1, -1):
        a = mon - dt.timedelta(7 * i)
        b = a + dt.timedelta(6)
        inw = [r for r in al if a.isoformat() <= r['date'] <= b.isoformat()]
        hrs = lambda sp: jr(sum((r['dur_s'] or 0) for r in inw if r['sport'] in sp) / 3600, 1)
        weeks.append({'week': (a - w0).days // 7, 'start': a.isoformat(), 'swim_h': hrs(('swim',)), 'bike_h': hrs(('bike',)), 'run_h': hrs(('run',)),
                      'strength_mobility_h': hrs(('strength', 'mobility')), 'other_h': hrs(('other',)),
                      'total_h': hrs(('swim', 'bike', 'run', 'strength', 'mobility', 'other')), 'tss': int(sum(jr(tss(r)) for r in inw))})
    return {'pmc': {'today': p(today), 'ctl_7d_ago': p(ago(7))[0], 'ctl_14d_ago': p(ago(14))[0], 'ctl_28d_ago': p(ago(28))[0], 'ctl_42d_ago': p(ago(42))[0],
                    'peak_ctl': [peak, pmc[peak][0]] if peak else None, 'lowest_form_28d': [low, pmc[low][2]] if low else None,
                    'tss_last_7d': tss_in(ago(7), today), 'tss_prev_7d': tss_in(ago(14), ago(7))},
            'efride': eff('bike'), 'efrun': eff('run'), 'dec': dec, 'sleep_28n': sleep, 'hrv_60d': hrv, 'rhr_60d': rhr, 'weeks': weeks}


def readiness(dl):
    # Same rule as the dashboard: 2 of the last 3 nights under 6.5 h; Garmin HRV status LOW or POOR;
    # resting HR >= baseline + 5 three days running (baseline = median of the 28 days ending 3 days back).
    out = []
    for i, r in enumerate(dl):
        prior = sorted(x['rhr'] for x in dl[max(0, i - 30):max(0, i - 2)] if x.get('rhr') is not None)
        base = (prior[(len(prior) - 1) // 2] + prior[len(prior) // 2]) / 2 if len(prior) >= 7 else None
        short = r.get('sleep_s') is not None and r['sleep_s'] < 6.5 * 3600
        up = r.get('rhr') is not None and base is not None and r['rhr'] >= base + 5
        out.append({'date': r['date'], 'synced': r.get('sleep_s') is not None or r.get('hrv') is not None,
                    'sleep_h': round(r['sleep_s'] / 3600, 2) if r.get('sleep_s') else None, 'short': short,
                    'hrv': r.get('hrv'), 'hrv_status': r.get('hrv_status'), 'rhr': r.get('rhr'), 'rhr_base': base, 'rhr_up': up})
    for i, d in enumerate(out):
        last3 = out[max(0, i - 2):i + 1]
        d['short3'] = sum(1 for x in last3 if x['short'])
        flags = []
        if d['short3'] >= 2:
            flags.append('sleep')
        if len(last3) == 3 and all(x['rhr_up'] for x in last3):
            flags.append('resting HR')
        if d['hrv_status'] in ('LOW', 'POOR'):
            flags.append('HRV')
        d['flags'] = flags
        d['flag_run'] = (out[i - 1]['flag_run'] if i else 0) + 1 if flags else 0
        d['verdict'] = 'Recovery-week version' if d['flag_run'] >= 3 else 'Quality becomes easy' if flags else 'Train as planned'
    return [d for d in out if d['synced']]


# ---------- plan (the dashboard's plan file: sessions, weeks, targets) ----------
TKEYS = {'ftp_w': 'ftp', 'lthr': 'lthr', 'css_s': 'css_s', 'hr_rest': 'hr_rest', 'hr_max': 'hr_max',
         'z2_hr_cap': 'cap', 'z2_hr_low': 'z2_low', 'modified_pct': 'mod_pct'}
CLI = {'--ftp': 'ftp', '--lthr': 'lthr', '--css': 'css_s', '--hrrest': 'hr_rest', '--hrmax': 'hr_max'}
DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

def apply_targets(plan, today):
    # The latest dated row of each target up to today, as the dashboard reads them; a value given on the command line wins.
    if not plan:
        return
    for key, tk in TKEYS.items():
        rows = [r for r in plan.get('targets', []) if r.get('key') == key and str(r.get('from') or '') <= today]
        if rows and not any(c in ARGS for c, v in CLI.items() if v == tk):
            T[tk] = float(max(rows, key=lambda r: str(r.get('from') or ''))['value'])

def act_sport(r):  # the dashboard's rule: strength sessions named for mobility count as mobility
    return 'mobility' if r['sport'] == 'strength' and re.search(r'mobility|stretch|yoga|flexib', r.get('name') or '', re.I) else r['sport']

def plan_status(plan, al, today):
    """Each planned session matched to Garmin the way the dashboard's Tracker does it (Garmin only, without his spreadsheet log)."""
    wks = [(int(w['week']), w['start'], w['end']) for w in plan['weeks']]
    week_of = lambda d: next((n for n, a, b in wks if a <= d <= b), None)
    want = lambda s: s if s in ('swim', 'bike', 'run', 'strength', 'mobility') else None
    yes = lambda v: v in (True, 'true', 'True', 1, '1')
    pool = [dict(r, sport=act_sport(r), week=week_of(r['date'])) for r in al if act_sport(r) in ('swim', 'bike', 'run', 'strength', 'mobility')]
    rows = [dict(s, week=int(s['week']), minutes=float(s.get('minutes') or 0), yards=float(s.get('yards') or 0),
                 key=yes(s.get('key')), optional=yes(s.get('optional')), match=None) for s in plan['sessions']]
    used = set()
    for pas in ('day', 'week'):
        for s in rows:
            if s['match'] or not want(s['sport']) or (pas == 'week' and s['optional']):
                continue
            c = [a for a in pool if a['id'] not in used and a['sport'] == s['sport'] and
                 (a['date'] == s['date'] if pas == 'day' else (a['week'] == s['week'] and a['date'] != s['date']))]
            if pas == 'day':
                c.sort(key=lambda a: a['start'], reverse=s.get('slot') == 'PM')
            else:
                c.sort(key=lambda a: abs((dt.date.fromisoformat(a['date']) - dt.date.fromisoformat(s['date'])).days))
            if c:
                s['match'] = c[0]
                used.add(c[0]['id'])
    mod = T.get('mod_pct')
    out = []
    for s in rows:
        a = s['match']
        by_yd = s['sport'] == 'swim' and s['yards'] > 0
        if not want(s['sport']):
            status = 'Rest' if s['sport'] == 'rest' else 'Note'
        elif a:
            plan_amt = s['yards'] if by_yd else s['minutes']
            act_amt = (a.get('yd') or 0) if by_yd else (a.get('dur_s') or 0) / 60
            status = 'Modified' if mod and plan_amt > 0 and act_amt < plan_amt * mod / 100 else 'Done' if a['date'] == s['date'] else 'Moved'
        elif s['date'] < today:
            status = 'Optional' if s['optional'] else 'Not recorded' if s['sport'] == 'mobility' else 'Skipped'
        else:
            status = 'Today' if s['date'] == today else 'Planned'
        out.append((s, a, status))
    return out

def plan_summary(plan, al, pmc, load, today):
    """The plan rows the report needs: yesterday, today, tomorrow, this week, and last week on Mondays."""
    if not plan:
        return None
    td = dt.date.fromisoformat(today)
    iso = lambda n: (td + dt.timedelta(n)).isoformat()
    rows = plan_status(plan, al, today)
    weeks = {int(w['week']): w for w in plan['weeks']}
    grp = lambda sp: 'sm' if sp in ('strength', 'mobility') else sp if sp in ('swim', 'bike', 'run') else None
    pace = lambda mps: '%d:%02d' % divmod(int(round(1609.344 / mps)), 60) if mps else None
    def row(s, a, status, full):
        r = {'date': s['date'], 'dow': s.get('dow'), 'slot': s.get('slot') or '', 'sport': s['sport'], 'code': s.get('code') or '',
             'session': s.get('tracker_label') or s.get('code') or s['sport'], 'minutes': round(s['minutes']), 'status': status}
        for k in ('yards',):
            if s[k]:
                r[k] = round(s[k])
        if s['key']:
            r['key'] = True
        if s['optional']:
            r['optional'] = True
        if full and s.get('text'):
            r['text'] = s['text']
        if a:
            r['recorded'] = {'id': a['id'], 'date': a['date'], 'start': a['start'], 'name': a['name'], 'min': round((a.get('dur_s') or 0) / 60)}
            if a.get('yd'):
                r['recorded']['yd'] = a['yd']
        return r
    day_rows = lambda d, full: [row(s, a, x, full) for s, a, x in rows if s['date'] == d]
    def week_block(n, full):
        w = weeks.get(n)
        if not w:
            return None
        rs = [(s, a, x) for s, a, x in rows if s['week'] == n]
        plan_h = {g: 0.0 for g in ('swim', 'bike', 'run', 'sm')}
        done_h = dict(plan_h)
        for s, a, x in rs:
            if grp(s['sport']):
                plan_h[grp(s['sport'])] += s['minutes'] / 60
        for r in al:
            if w['start'] <= r['date'] <= w['end'] and grp(act_sport(r)):
                done_h[grp(act_sport(r))] += (r.get('dur_s') or 0) / 3600
        scope = [(s, a, x) for s, a, x in rs if x in ('Done', 'Moved', 'Modified', 'Skipped')]
        count = lambda st_: sum(1 for s, a, x in scope if x == st_)
        last = min(w['end'], today)
        prev = (dt.date.fromisoformat(w['start']) - dt.timedelta(1)).isoformat()
        b = {'week': n, 'title': w.get('title'), 'start': w['start'], 'end': w['end'], 'plan_h': round(float(w.get('plan_hours') or 0), 2),
             'plan_h_by_sport': {g: round(v, 2) for g, v in plan_h.items()},
             'done_h_by_sport': {g: round(v, 2) for g, v in done_h.items()}, 'done_h': round(sum(done_h.values()), 2),
             'tss': int(sum(v for k, v in load.items() if w['start'] <= k <= last)),
             'fitness_start': (pmc.get(prev) or (None,))[0], 'fitness_end': (pmc.get(last) or (None,))[0],
             'sessions': {'done': count('Done'), 'moved': count('Moved'), 'modified': count('Modified'), 'skipped': count('Skipped')},
             'key': {'total': sum(1 for s, a, x in rs if s['key']),
                     'done': sum(1 for s, a, x in rs if s['key'] and x in ('Done', 'Moved', 'Modified'))},
             'by_session': [{'date': s['date'], 'dow': s.get('dow'), 'slot': s.get('slot') or '', 'code': s.get('code') or '',
                             'session': s.get('tracker_label') or s.get('code') or s['sport'], 'minutes': round(s['minutes']), 'status': x,
                             **({'recorded_on': a['date']} if a and a['date'] != s['date'] else {})}
                            for s, a, x in rs if grp(s['sport'])]}
        if full:
            b['focus'] = w.get('focus')
        bench = {}
        for sport, code in (('bike', 'BK-E'), ('run', 'RN-E')):
            hit = next((a for s, a, x in rs if s['sport'] == sport and s.get('code') == code and a and
                        dt.date.fromisoformat(s['date']).weekday() == 3), None)
            if hit:
                bench[sport] = {'date': hit['date'], 'main_min': round((hit.get('main_s') or 0) / 60), 'main_hr': hit.get('main_hr'), 'ef': hit.get('ef'),
                                'main_cad': hit.get('main_cad'),
                                **({'main_w': hit.get('main_w'), 'main_np': hit.get('main_np')} if sport == 'bike' else {'pace_mi': pace(hit.get('main_mps'))})}
        b['thursday_benchmark'] = bench
        return b
    now_wk = next((int(w['week']) for w in plan['weeks'] if w['start'] <= today <= w['end']), None)
    out = {'yesterday': day_rows(iso(-1), False), 'today': day_rows(today, True), 'tomorrow': day_rows(iso(1), True),
           'this_week': week_block(now_wk, True) if now_wk is not None else None}
    if td.weekday() == 0 and now_wk is not None:
        out['last_week'] = week_block(now_wk - 1, False)
    return out


# ---------- workout reviews: each new swim, ride and run, lap by lap ----------
ZONES = [('z1', 0, 141), ('z2', 142, 152), ('z3', 153, 163), ('z4', 164, 172), ('z5', 173, 999)]  # plan HR zones (LTHR 172)

def mmss(s):
    return None if s is None else '%d:%02d' % divmod(int(round(s)), 60)

def step_target(stp):
    """A workout step's target as text: power in W, HR in bpm (other targets are left to the plan text)."""
    if not stp or stp.get('lo') is None or stp.get('hi') is None:
        return None
    t, lo, hi = str(stp.get('type') or ''), stp['lo'], stp['hi']
    if 'power' in t:
        lo, hi = (lo - 1000, hi - 1000) if lo > 1000 else (lo, hi)
        return '%d–%d W' % (lo, hi) if lo >= 50 else None
    if 'heart_rate' in t and lo > 100:
        return '%d–%d bpm' % (lo - 100, hi - 100)
    return None

def fit_detail(data, r):
    """Lap by lap (rides, runs) or rep by rep (swims), for the workout's review."""
    recs, laps, steps, sess = fit_read(data)
    sport = act_sport(r)
    out = {'hr_source': 'chest strap' if sess.get('strap') else 'wrist'}
    hrs = [x[2] for x in resample(recs) if x[2]] if recs else []
    if hrs:
        out['hr_zones_min'] = {z: round(sum(1 for h in hrs if a <= h <= b) / 60, 1) for z, a, b in ZONES}
    laps = sorted([l for l in laps if l['start'] is not None], key=lambda l: l['start'])
    if sport == 'swim':
        lens = [x for x in sess.get('lengths') or [] if x['start'] is not None and x['active']]
        reps, rest = [], 0.0
        for l in laps:
            if not l.get('dist'):
                rest += l['timer'] or 0
                if reps:
                    reps[-1][-1] = int(round(reps[-1][-1] + (l['timer'] or 0)))
                continue
            a, z = l['start'] - 1, l['start'] + (l['elapsed'] or l['timer'])
            ln = [x for x in lens if a <= x['start'] < z]
            strokes = [x['strokes'] for x in ln if x.get('strokes')]
            kinds = [str(x['stroke']) for x in ln if x.get('stroke') is not None] or ([str(l['stroke'])] if l.get('stroke') else [])
            stroke = max(set(kinds), key=kinds.count) if kinds else None
            yd = int(round(l['dist'] / 0.9144))
            spl = round(st.mean(strokes), 1) if strokes else None
            sec_len = st.mean(x['sec'] for x in ln) if ln else None
            reps.append([len(reps) + 1, yd, round(l['timer'], 1), mmss(l['timer'] / yd * 100) if yd else None, stroke, spl,
                         int(round(sec_len + spl)) if spl is not None and sec_len else None, l.get('hr'), 0])
        out.update({'reps_cols': ['n', 'yd', 'sec', 'per_100yd', 'stroke', 'strokes_per_length', 'swolf', 'hr', 'rest_after_s'],
                    'reps': reps, 'swim_yd': sum(x[1] for x in reps), 'swim_min': round(sum(x[2] for x in reps) / 60, 1),
                    'rest_min': round(rest / 60, 1)})
        return out
    run = sport == 'run'
    k = 1.0
    if run and sess.get('dist') and sess.get('rec_dist') and abs(sess['dist'] / sess['rec_dist'] - 1) > 0.02:
        k = sess['dist'] / sess['rec_dist']
    if k != 1.0:
        out['treadmill_scale'] = round(k, 3)
    rows = []
    for i, l in enumerate(laps):
        stp = steps.get(l['step']) if l['step'] is not None else None
        kind = l['intensity'] if l['intensity'] is not None else (stp or {}).get('intensity')
        spd = (l.get('spd') or 0) * k
        cad = l.get('cad')
        rows.append([i + 1, str(kind) if kind is not None else None, (stp or {}).get('name'), step_target(stp),
                     round((l['timer'] or 0) / 60, 1), round((l.get('dist') or 0) * k / 1609.344, 2), l.get('w'), l.get('np'),
                     l.get('hr'), l.get('hr_max'), cad * 2 if run and cad else cad,
                     mmss(1609.344 / spd) if run and spd else None,
                     round(spd * 2.23694, 1) if not run and spd and not r.get('indoor') else None])
    out.update({'laps_cols': ['n', 'kind', 'step', 'target', 'min', 'mi', 'avg_w', 'np', 'hr', 'hr_max', 'cad', 'pace_mi', 'mph'],
                'laps': rows})
    return out

def review_ids(acts, reports, today):
    """New swims, rides and runs to review: from REVIEW_FROM and the last REVIEW_DAYS days, not reviewed in an earlier report."""
    done = {str(w.get('id')) for rep in reports or [] if isinstance(rep, dict)
            for w in (rep.get('workouts') or []) if isinstance(w, dict)}
    start = max(REVIEW_FROM, (dt.date.fromisoformat(today) - dt.timedelta(REVIEW_DAYS)).isoformat())
    c = [r for r in acts.values() if act_sport(r) in ('swim', 'bike', 'run') and not r.get('race') and start <= r['date'] <= today
         and (r['dur_s'] or 0) >= REVIEW_MIN_S and r['id'] not in done]
    c.sort(key=lambda r: (r['date'], r['start']), reverse=True)
    return [r['id'] for r in c[:REVIEW_MAX]], [r['id'] for r in c[REVIEW_MAX:]]

def prev_row(a):
    sp = act_sport(a)
    p = {'date': a['date'], 'min': round((a['dur_s'] or 0) / 60)}
    if sp == 'swim':
        p.update({'yd': a.get('yd'), 'per_100yd_moving': mmss(a['mov_s'] / a['yd'] * 100) if a.get('yd') and a.get('mov_s') else None})
    elif sp == 'bike':
        p.update({k: a.get(k) for k in ('main_w', 'main_np', 'main_hr', 'main_cad', 'ef', 'dec')})
    else:
        p.update({'pace_mi': mmss(1609.344 / a['main_mps']) if a.get('main_mps') else None})
        p.update({k: a.get(k) for k in ('main_hr', 'main_cad', 'ef', 'dec')})
    return p

def review_entries(ids, al, plan, ready, fits, today):
    """Everything the review of each new workout needs: plan row, Garmin's numbers, the FIT detail and comparisons."""
    rows = plan_status(plan, al, today) if plan else []
    by_act = {a['id']: (s, x) for s, a, x in rows if a}
    code_of = {aid: (s.get('code') or '') for aid, (s, x) in by_act.items()}
    rd = {d['date']: d for d in ready}
    acts = {r['id']: r for r in al}
    out = []
    for aid in ids:
        r = acts[aid]
        sp = act_sport(r)
        e = {'id': aid, 'date': r['date'], 'dow': DOW[dt.date.fromisoformat(r['date']).weekday()], 'start': r['start'], 'sport': sp,
             'type': r['type'], 'indoor': r['indoor'], 'name': r['name'], 'min': round((r['dur_s'] or 0) / 60),
             'moving_min': round((r.get('mov_s') or 0) / 60), 'hr': r.get('hr'), 'hr_max': r.get('hr_max'), 'tss': int(round(tss(r)))}
        if sp == 'swim':
            e['yd'] = r.get('yd')
            e['per_100yd_moving'] = mmss(r['mov_s'] / r['yd'] * 100) if r.get('yd') and r.get('mov_s') else None
        else:
            e['mi'] = round((r.get('dist_m') or 0) / 1609.344, 2)
            if sp == 'bike':
                e.update({'avg_w': r.get('w'), 'np': r.get('np'), 'if': round(r['np'] / T['ftp'], 2) if r.get('np') else None,
                          'vi': round(r['np'] / r['w'], 2) if r.get('np') and r.get('w') else None, 'cad': r.get('cad')})
            else:
                e.update({'pace_mi': mmss(1609.344 / r['mps']) if r.get('mps') else None, 'cad': r.get('cad')})
            mb = {k: r.get(k) for k in ('method', 'main_s', 'main_hr', 'main_w', 'main_np', 'main_cad', 'ef', 'dec', 'over_cap_s', 'z2_s', 'under_z2_s')}
            if r.get('main_mps'):
                mb['pace_mi'] = mmss(1609.344 / r['main_mps'])
            e['main_block'] = mb if r.get('fit') else None
        sx = by_act.get(aid)
        if sx:
            s, x = sx
            e['plan'] = {'code': s.get('code'), 'session': s.get('tracker_label'), 'planned_date': s['date'], 'slot': s.get('slot'),
                         'minutes': round(s['minutes']), 'yards': round(s['yards']) or None, 'text': s.get('text'), 'status': x, 'key': s['key']}
        else:
            e['plan'] = None
        code = code_of.get(aid)
        before = lambda a: (a['date'], a['start']) < (r['date'], r['start'])
        e['same_session_before'] = [prev_row(a) for a in al if code and a['id'] != aid and before(a) and code_of.get(a['id']) == code][-3:]
        if sp in ('bike', 'run') and r.get('ef'):
            lo_s, lo_hr = (1200, 120) if sp == 'bike' else (900, 125)
            like = [a['ef'] for a in al if act_sport(a) == sp and not a.get('race') and a['indoor'] == r['indoor'] and a.get('ef')
                    and (a.get('main_s') or 0) >= lo_s and (a.get('main_hr') or 0) >= lo_hr and before(a)][-6:]
            med = st.median(like) if like else None
            e['ef_vs_prev6'] = {'ef': r['ef'], 'prev6_median': round(med, 3) if med else None,
                                'delta_pct': round((r['ef'] - med) / med * 100, 1) if med else None}
        d = rd.get(r['date'])
        if d:
            e['readiness_that_morning'] = {k: d.get(k) for k in ('sleep_h', 'hrv', 'hrv_status', 'rhr', 'rhr_base', 'verdict')}
        e['detail'] = fits.get(aid)
        out.append(e)
    return out


# ---------- publish: check today's report and add it to the history file ----------
SECTIONS = ['Last night', 'Yesterday', 'This morning', 'Training load', 'Next', 'Last week', "Coach's call"]
CHARTS = ['pmc', 'efride', 'efrun', 'dec', 'sleephours', 'sleepwindow', 'hrv', 'rhr', 'hours']
GRADES = ['On target', 'Mostly on target', 'Off target']

def check_workouts(ws, review_ids, err):
    if ws is None:
        ws = []
    if not isinstance(ws, list):
        err.append('workouts must be a list')
        return []
    got = [str(w.get('id')) for w in ws if isinstance(w, dict)]
    if len(got) != len(set(got)):
        err.append('workouts has the same id twice')
    miss = [x for x in review_ids if x not in got]
    extra = [x for x in got if x not in review_ids]
    if miss:
        err.append('workouts is missing a review for: %s' % miss)
    if extra:
        err.append('workouts has ids that are not in summary.reviews: %s' % extra)
    texts = []
    for w in ws:
        if not isinstance(w, dict):
            err.append('each workout review must be an object')
            continue
        wid = w.get('id')
        title, head = str(w.get('title') or ''), str(w.get('headline') or '')
        if not title.strip() or len(title) > 140:
            err.append('workout %s: title must be 1-140 characters' % wid)
        if w.get('grade') not in GRADES:
            err.append('workout %s: grade must be one of %s' % (wid, GRADES))
        if not head.strip() or len(head) > 240:
            err.append('workout %s: headline must be 1-240 characters' % wid)
        its = w.get('items') if isinstance(w.get('items'), list) else []
        if not 6 <= len(its) <= 12:
            err.append('workout %s: 6-12 items needed (now %d)' % (wid, len(its)))
        if any(not isinstance(i, dict) or not str(i.get('label') or '').strip() or not str(i.get('text') or '').strip() for i in its):
            err.append('workout %s: every item needs a label and a text' % wid)
        texts += [title, head] + [str(i.get('text') or '') for i in its if isinstance(i, dict)]
    return texts

def check_report(r, today, week, review_ids=()):
    err, warn = [], []
    td = dt.date.fromisoformat(today)
    if r.get('date') != today:
        err.append('date must be ' + today)
    if r.get('dow') != DOW[td.weekday()]:
        err.append('dow must be ' + DOW[td.weekday()])
    if week is not None and r.get('week') != week:
        err.append('week must be %d' % week)
    if r.get('status') not in ('On track', 'Watch', 'Act'):
        err.append('status must be "On track", "Watch" or "Act"')
    h = r.get('headline') or ''
    if not h or len(h) > 160:
        err.append('headline must be 1-160 characters (now %d)' % len(h))
    items = r.get('items') or []
    if not 8 <= len(items) <= 14:
        err.append('8-14 items needed (now %d)' % len(items))
    secs = [i.get('section') for i in items]
    bad = [s for s in secs if s not in SECTIONS]
    if bad:
        err.append('unknown sections: %s' % bad)
    elif secs != sorted(secs, key=SECTIONS.index):
        err.append('sections out of order; the order is: %s' % SECTIONS)
    if 'Last week' in secs and td.weekday() != 0:
        warn.append('"Last week" is for Mondays only')
    if any(not str(i.get('text') or '').strip() for i in items):
        err.append('an item has no text')
    cn = r.get('chart_notes')
    if cn is not None:
        if not isinstance(cn, dict) or set(cn) != set(CHARTS):
            err.append('chart_notes needs exactly these keys: %s' % CHARTS)
        else:
            for k in CHARTS:
                n = len(re.findall(r'[.!?](?:\s|$)', str(cn[k] or '')))
                if not 3 <= n <= 6:
                    warn.append('chart note %s has %d sentences (3-6 wanted)' % (k, n))
    wtexts = check_workouts(r.get('workouts'), [str(x) for x in review_ids], err)
    text = ' '.join([h] + [str(i.get('text') or '') for i in items] + ([str(v) for v in cn.values()] if isinstance(cn, dict) else []) + wtexts)
    for d, m, day in re.findall(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (%s) (\d{1,2})\b' % '|'.join(MONTHS), text):
        cands = []
        for y in (td.year - 1, td.year, td.year + 1):
            try:
                cands.append(dt.date(y, MONTHS.index(m) + 1, int(day)))
            except ValueError:
                pass
        if cands:
            real = min(cands, key=lambda c: abs((c - td).days))
            if DOW[real.weekday()] != d:
                err.append('"%s %s %s": %s %s is a %s' % (d, m, day, m, day, DOW[real.weekday()]))
    for p in ('line not to cross', 'clock more than the hours', ' in bed'):
        if p in text:
            warn.append('shorthand to rewrite in plain words: "%s"' % p.strip())
    return err, warn

def publish(path):
    r = json.load(open(path, encoding='utf-8'))
    state = json.load(open('run_state.json', encoding='utf-8'))
    err, warn = check_report(r, state['today'], state.get('week'), state.get('review_ids') or [])
    if err:
        print(json.dumps({'ok': False, 'errors': err, 'warnings': warn}, ensure_ascii=False))
        sys.exit(1)
    h = json.load(open('history.json', encoding='utf-8'))
    reps = [x for x in (h.get('reports') or []) if x.get('date') != r['date']]
    h['reports'] = sorted([r] + reps, key=lambda x: x.get('date') or '', reverse=True)[:120]
    json.dump(h, open('history.json', 'w', encoding='utf-8'), separators=(',', ':'), ensure_ascii=False)
    print(json.dumps({'ok': True, 'warnings': warn, 'reports_kept': len(h['reports'])}, ensure_ascii=False))


def main():
    mode = ARGS[0] if ARGS else 'build'
    if mode == 'publish':
        publish(ARGS[1] if len(ARGS) > 1 else 'report.json')
        return
    found = classify()
    acts, days = merge(found)
    today = max(days) if days else dt.date.today().isoformat()
    apply_targets(found['plan'], today)
    since = (dt.date.fromisoformat(today) - dt.timedelta(21)).isoformat()
    reports = (found['old'] or {}).get('reports') or found['reports_list'] or []
    rid, rskip = review_ids(acts, reports, today)
    if mode == 'need':
        nf = [x for x in rid if x not in found['fit']]
        nf += [r['id'] for r in acts.values() if needs_fit(r, since) and r['id'] not in found['fit'] and r['id'] not in nf]
        print(json.dumps({'old_history': bool(found['old']), 'plan': bool(found['plan']), 'activity_lists': len(found['acts']),
                          'daily': len(found['daily']), 'hrv': len(found['hrv']), 'sleep': len(found['sleep']),
                          'to_review': rid, 'need_fit': nf}))
        return
    if not found['old']:
        print(json.dumps({'error': 'No history file found, so nothing was built: a rebuild without it would lose the FIT history.'}))
        sys.exit(1)
    for aid, data in found['fit'].items():
        if aid in acts and not acts[aid].get('fit') and acts[aid]['sport'] in ('bike', 'run'):
            try:
                m = fit_metrics(data)
                if m:
                    acts[aid].update(m)
            except Exception as e:  # a bad file leaves that session without FIT metrics
                print('FIT', aid, repr(e), file=sys.stderr)
    fits = {}
    for aid in rid:
        if aid in found['fit']:
            try:
                fits[aid] = fit_detail(found['fit'][aid], acts[aid])
            except Exception as e:  # the review then uses Garmin's summary numbers only
                print('FIT detail', aid, repr(e), file=sys.stderr)
    al = sorted(acts.values(), key=lambda r: (r['date'], r['start']))
    dl = [days[k] for k in sorted(days) if k >= '2026-07-01']
    built = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    json.dump({'built_at': built, 'acts': al, 'days': dl, 'reports': reports}, open('history.json', 'w', encoding='utf-8'),
              separators=(',', ':'), ensure_ascii=False)
    out = summary(acts, days, today, found['plan'])
    week = next((int(w['week']) for w in (found['plan'] or {}).get('weeks', []) if w['start'] <= today <= w['end']), None)
    json.dump({'today': today, 'week': week, 'review_ids': rid}, open('run_state.json', 'w'))
    out['reviews'] = review_entries(rid, al, found['plan'], readiness([days[k] for k in sorted(days)]), fits, today)
    out['reviews_skipped'] = [{'id': x, 'date': acts[x]['date'], 'name': acts[x]['name']} for x in rskip]
    out.update({'script_version': SCRIPT_VERSION, 'built_at': built, 'acts': len(al), 'days': len(dl), 'reports_kept': len(reports),
                'plan_file': bool(found['plan']), 'still_need_fit': [r['id'] for r in al if needs_fit(r, since)]})
    print(json.dumps(out, separators=(',', ':'), ensure_ascii=False))


if __name__ == '__main__':
    main()
