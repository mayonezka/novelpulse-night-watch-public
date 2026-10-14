"""Original procedural soundtrack: no downloaded music, samples or sound licences."""
import math
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 24000


def synthesize_soundtrack(directory, duration, system_starts=()):
    if not 0 < duration <= 60:
        raise ValueError('Soundtrack duration must be in (0, 60]')
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    bed, effects = array('h'), array('h')
    # Quantized cycles make the minor chord and its pulse phase-continuous at the cut.
    frequencies = [round(f * duration) / duration for f in (65.406, 77.782, 97.999)]
    beats = max(1, round(duration * 1.1))
    for i in range(round(duration * SAMPLE_RATE)):
        t = i / SAMPLE_RATE
        phase = 2 * math.pi * beats * t / duration
        pulse = .72 + .28 * math.sin(phase)
        drone = sum(math.sin(2 * math.pi * f * t) for f in frequencies) / 3
        heartbeat = max(0, math.sin(phase)) ** 12 * math.sin(2 * math.pi * frequencies[0] * t)
        music = .30 * drone * pulse + .15 * heartbeat
        fx = 0.0
        if t < .9:
            fx += .62 * math.exp(-6*t) * math.sin(2*math.pi*(62*t-14*t*t))
            fx += .10 * math.exp(-17*t) * math.sin(2*math.pi*(1400*t+3000*t*t))
        for start in system_starts:
            dt = t - start
            if 0 <= dt < .65:
                fx += .20 * math.exp(-9*dt) * (math.sin(2*math.pi*1047*dt)+.4*math.sin(2*math.pi*2093*dt))
        if duration-3.5 <= t:
            dt = (t-(duration-3.5)) % .28
            fx += .16 * math.exp(-70*dt) * math.sin(2*math.pi*1900*dt)
        # Prevent clicks at the exact edit/loop boundary, including the first impact.
        fade = min(1, t/.005, (duration-t)/.012)
        bed.append(int(max(-1,min(1,music))*32767))
        effects.append(int(max(-1,min(1,fx*fade))*32767))
    paths=[]
    for name,samples in (('tension.wav',bed),('effects.wav',effects)):
        path=root/name
        with wave.open(str(path),'wb') as stream:
            stream.setparams((1,2,SAMPLE_RATE,0,'NONE','not compressed'))
            stream.writeframes(samples.tobytes())
        paths.append(path)
    return paths


async def mix_soundtrack(speech, directory, duration, system_starts, tempo, ffmpeg):
    import asyncio
    bed,effects=await asyncio.to_thread(synthesize_soundtrack,directory,duration,system_starts)
    output=Path(directory)/'mixed.wav'
    await ffmpeg('-y','-v','error','-i',str(speech),'-i',str(bed),'-i',str(effects),
        '-filter_complex',f'[0:a]atempo={tempo:.8f},volume=1.15[v];'
            '[1:a]volume=-18dB[b];[v][b][2:a]amix=inputs=3:duration=first:normalize=0,'
            'alimiter=limit=0.94:latency=1[out]',
        '-map','[out]','-ar',str(SAMPLE_RATE),'-ac','2','-t',f'{duration:.8f}',str(output))
    return output
