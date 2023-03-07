import os
import pathlib

from multiprocessing import Pool
from typing import List, Tuple

import numpy as np
import soundfile as sf

from src.file_helper import FileHelper
from src.misc_helper import gen_hour_minute_times
from src.pypam_support import pypam_process


class ProcessHelper:
    def __init__(
        self,
        file_helper: FileHelper,
        output_dir: str,
        save_extracted_wav: bool = False,
        num_cpus: int = 0,
    ):
        self.file_helper = file_helper
        self.output_dir = output_dir
        self.save_extracted_wav = save_extracted_wav
        self.num_cpus = _get_cpus_to_use(num_cpus)

        pathlib.Path(output_dir).mkdir(exist_ok=True)

    def process_day(self, year: int, month: int, day: int):
        if not self.file_helper.select_day(year, month, day):
            return

        at_hour_and_minutes: List[Tuple[int, int]] = list(
            gen_hour_minute_times(self.file_helper.segment_size_in_mins)
        )

        if self.num_cpus > 1:
            splits = np.array_split(at_hour_and_minutes, self.num_cpus)
            print(
                f"Splitting {len(at_hour_and_minutes)} segments into {len(splits)} processes ..."
            )
            with Pool(self.num_cpus) as pool:
                args = [(s,) for s in splits]
                pool.starmap(self.process_hours_minutes, args)

        else:
            self.process_hours_minutes(at_hour_and_minutes)

    def process_hours_minutes(self, hour_and_minutes: List[Tuple[int, int]]):
        for at_hour, at_minute in hour_and_minutes:
            self.process_hour_minute(at_hour, at_minute)

    def process_hour_minute(self, at_hour: int, at_minute: int):
        # TODO capture info for "effort" variable, in particular,
        #  the number of seconds of actual data per segment

        file_helper = self.file_helper
        year, month, day = file_helper.year, file_helper.month, file_helper.day

        print(f"\nSegment at {at_hour:02}h:{at_minute:02}m ...")
        print(f"  - extracting {file_helper.segment_size_in_mins * 60}-sec segment:")
        extraction = file_helper.extract_audio_segment(at_hour, at_minute)
        # TODO properly consider the 3 possible cases for the segment:
        #   whole data, partial data, no data
        # for now, assuming whole minute segment
        if extraction is None:
            print(f"ERROR: cannot get audio segment at {at_hour:02}:{at_minute:02}")
            return

        audio_info, audio_segment = extraction

        if self.save_extracted_wav:
            wav_filename = f"{self.output_dir}/extracted_{year:04}{month:02}{day:02}_{at_hour:02}{at_minute:02}00.wav"
            sf.write(
                wav_filename, audio_segment, audio_info.samplerate, audio_info.subtype
            )
            print(f"  √ saved extracted wav: {wav_filename} len={len(audio_segment):,}")

        audio_segment *= 3  # voltage multiplier

        # print(f"  √ segment loaded, len = {len(audio_segment):,}")
        print("  - processing ...")
        milli_psd = pypam_process(audio_info.samplerate, audio_segment)

        # Note: preliminary naming for output, etc.
        netcdf_filename = f"{self.output_dir}/milli_psd_{year:04}{month:02}{day:02}_{at_hour:02}{at_minute:02}00.nc"
        print(f"  - saving milli_psd result to {netcdf_filename}")
        milli_psd.to_netcdf(netcdf_filename)
        # on my Mac: format='NETCDF4_CLASSIC' triggers:
        #    ValueError: invalid format for scipy.io.netcdf backend: 'NETCDF4_CLASSIC'


def _get_cpus_to_use(num_cpus: int) -> int:
    cpu_count = os.cpu_count()
    if num_cpus <= 0 and cpu_count is not None:
        num_cpus = cpu_count
    if cpu_count is not None and num_cpus > cpu_count:
        num_cpus = cpu_count
    return num_cpus
