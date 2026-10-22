import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import audio_overlap


def packets(values):
    return [{'time':i*0.02,'duration':0.02,'sha256':hashlib.sha256(str(v).encode()).hexdigest()}
            for i,v in enumerate(values)]


class AudioOverlapTests(unittest.TestCase):
    def test_exact_varied_suffix_prefix_match_proves_overlap(self):
        result=audio_overlap.confirm_overlap(packets(range(200)),packets(range(100,400)),2)
        self.assertEqual(result['status'],'confirmed_exact_audio')
        self.assertEqual(result['matched_packets'],100)
        self.assertAlmostEqual(result['trim_seconds'],2)

    def test_silence_repetition_and_filename_timing_alone_never_trim(self):
        for left,right in [(packets([0]*200),packets([0]*200)),
                           (packets(range(200)),packets(range(500,800)))]:
            result=audio_overlap.confirm_overlap(left,right,2)
            self.assertEqual(result['status'],'not_confirmed')
            self.assertEqual(result['trim_seconds'],0)

    def test_shorter_audio_match_does_not_trim_the_longer_container_estimate(self):
        result=audio_overlap.confirm_overlap(packets(range(200)),packets(range(100,400)),5)
        self.assertEqual(result['status'],'confirmed_exact_audio')
        self.assertEqual(result['trim_seconds'],2)
        self.assertEqual(len(result['matched_sequence_sha256']),64)

    def test_matching_packets_with_inconsistent_timing_are_preserved(self):
        left=packets(range(200));right=packets(range(100,400))
        for packet in right:
            if packet['time']>0.5:
                packet['time']+=0.5
        result=audio_overlap.confirm_overlap(left,right,2)
        self.assertFalse(result['packet_timing_consistent'])
        self.assertEqual(result['trim_seconds'],0)

    @unittest.skipUnless(shutil.which('ffmpeg'),'requires real FFmpeg')
    def test_real_aac_packet_overlap_is_verified_without_transcription(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'all.aac';left=root/'left.aac';right=root/'right.aac'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'anoisesrc=color=white:seed=42:r=48000','-t','10','-c:a','aac',str(source)],check=True,timeout=60)
            for target,start,length in [(left,0,7),(right,5,5)]:
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-ss',str(start),'-i',str(source),
                    '-t',str(length),'-c','copy',str(target)],check=True,timeout=60)
            result=audio_overlap.confirm_overlap(audio_overlap.packet_window(left,'tail'),audio_overlap.packet_window(right,'head'),2)
            self.assertEqual(result['status'],'confirmed_exact_audio')
            self.assertAlmostEqual(result['trim_seconds'],2,delta=0.1)
