import json
import pathlib
from datetime import datetime

# from multiprocessing import Pool
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf
import xarray as xr

from src import get_cpus_to_use, save_csv, save_dataset_to_netcdf, save_netcdf

from src.file_helper import FileHelper
from src.metadata import MetadataHelper
from src.misc_helper import debug, error, gen_hour_minute_times, info, warn
from src.pypam_support import PypamSupport


class ProcessHelper:
    def __init__(
        self,
        file_helper: FileHelper,
        output_dir: str,
        gen_csv: bool,
        global_attrs_uri: Optional[str] = None,
        variable_attrs_uri: Optional[str] = None,
        voltage_multiplier: Optional[float] = None,
        sensitivity_uri: Optional[str] = None,
        sensitivity_flat_value: Optional[float] = None,
        save_segment_result: bool = False,
        save_extracted_wav: bool = False,
        num_cpus: int = 1,
        max_segments: int = 0,
        subset_to: Optional[Tuple[int, int]] = None,
    ):
        """

        :param file_helper:
        :param output_dir:
        :param gen_csv:
        :param global_attrs_uri:
        :param variable_attrs_uri:
        :param voltage_multiplier:
        :param sensitivity_uri:
        :param sensitivity_flat_value:
        :param save_segment_result:
        :param save_extracted_wav:
        :param num_cpus:
        :param max_segments:
        :param subset_to:
            Tuple of (lower, upper) frequency limits to use for the PSD,
            lower inclusive, upper exclusive.
        """

        self.file_helper = file_helper
        self.output_dir = output_dir
        self.gen_csv = gen_csv

        self.metadata_helper = MetadataHelper(
            self._load_attributes("global", global_attrs_uri),
            self._load_attributes("variable", variable_attrs_uri),
        )

        self.save_segment_result = save_segment_result
        self.save_extracted_wav = save_extracted_wav
        self.num_cpus = get_cpus_to_use(num_cpus)
        self.max_segments = max_segments
        self.subset_to = subset_to

        self.voltage_multiplier: Optional[float] = voltage_multiplier

        self.sensitivity_da: Optional[xr.DataArray] = None
        self.sensitivity_flat_value: Optional[float] = sensitivity_flat_value

        if sensitivity_uri is not None:
            s_local_filename = file_helper.get_local_filename(sensitivity_uri)
            if s_local_filename is not None:
                sensitivity_ds = xr.open_dataset(s_local_filename)
                info(f"Will use loaded sensitivity from {s_local_filename=}")
                self.sensitivity_da = sensitivity_ds.sensitivity
                debug(f"{self.sensitivity_da=}")
            else:
                error(
                    f"Unable to resolve sensitivity_uri: '{sensitivity_uri}'. Ignoring it."
                )

        if self.sensitivity_da is None and self.sensitivity_flat_value is not None:
            info(f"Will use given flat sensitivity value: {sensitivity_flat_value}")

        # obtained once upon first segment to be processed
        self.pypam_support: Optional[PypamSupport] = None

        pathlib.Path(output_dir).mkdir(exist_ok=True)

    def _load_attributes(
        self, what: str, attrs_uri: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        if attrs_uri:
            info(f"Loading {what} attributes from {attrs_uri=}")
            filename = self.file_helper.get_local_filename(attrs_uri)
            if filename is not None:
                with open(filename, "r", encoding="UTF-8") as f:
                    return json.load(f)
            else:
                error(f"Unable to resolve '{attrs_uri=}'. Ignoring it.")
        else:
            info(f"No '{what}' attributes URI given.")
        return None

    def process_day(self, year: int, month: int, day: int) -> Optional[str]:
        """
        :param year:
        :param month:
        :param day:
        :return: The path of the generated NetCDF file, or None if no segments were processed.
        """
        if not self.file_helper.select_day(year, month, day):
            return None

        at_hour_and_minutes: List[Tuple[int, int]] = list(
            gen_hour_minute_times(self.file_helper.segment_size_in_mins)
        )

        if self.max_segments > 0:
            at_hour_and_minutes = at_hour_and_minutes[: self.max_segments]
            info(f"NOTE: Limiting to {len(at_hour_and_minutes)} segments ...")

        if self.num_cpus > 1:
            # TODO appropriate dispatch to then aggregate results
            info("NOTE: ignoring multiprocessing while completing aggregation of day")
            # splits = np.array_split(at_hour_and_minutes, self.num_cpus)
            # info(
            #     f"Splitting {len(at_hour_and_minutes)} segments into {len(splits)} processes ..."
            # )
            # with Pool(self.num_cpus) as pool:
            #     args = [(s,) for s in splits]
            #     pool.starmap(self.process_hours_minutes, args)
            # return

        self.process_hours_minutes(at_hour_and_minutes)
        if self.pypam_support is None:
            warn("No segments processed, nothing to aggregate.")
            return None

        md_helper = self.metadata_helper

        # get effort before calling get_aggregated_milli_psd, which will reset it:
        effort = self.pypam_support.get_effort()

        info("Aggregating results ...")
        aggregated_result = self.pypam_support.get_aggregated_milli_psd(
            sensitivity_da=self.sensitivity_da,
            sensitivity_flat_value=self.sensitivity_flat_value,
        )

        # rename 'frequency_bins' dimension to 'frequency':
        aggregated_result = aggregated_result.swap_dims(frequency_bins="frequency")

        data_vars = {
            "psd": aggregated_result,
            "effort": xr.DataArray(
                data=effort,
                dims=["time"],
                coords={"time": aggregated_result.time},
            ),
        }

        if self.sensitivity_da is not None:
            # TODO this case not yet tested
            freq_subset = self.sensitivity_da.interp(
                frequency=aggregated_result.frequency
            )
            data_vars["sensitivity"] = freq_subset

        elif self.sensitivity_flat_value is not None:
            # better way to capture a scalar?
            data_vars["sensitivity"] = xr.DataArray(
                data=[self.sensitivity_flat_value],
                dims=["1"],
            ).astype(np.float32)

            # If repeating the scalar for each frequency:
            # num_freqs = aggregated_result.frequency.shape[0]
            # data_vars["sensitivity"] = xr.DataArray(
            #     data=np.repeat(self.sensitivity_flat_value, num_freqs),
            #     dims=["frequency_bins"],
            #     coords={"frequency": aggregated_result.frequency},
            # ).astype(np.float32)

            md_helper.add_variable_attributes(data_vars["sensitivity"], "sensitivity")

        md_helper.add_variable_attributes(aggregated_result["time"], "time")
        md_helper.add_variable_attributes(aggregated_result["frequency"], "frequency")
        md_helper.add_variable_attributes(data_vars["psd"], "psd")
        md_helper.add_variable_attributes(data_vars["effort"], "effort")

        md_helper.set_global_attribute(
            "date_created", datetime.utcnow().strftime("%Y-%m-%d")
        )

        ds_result = xr.Dataset(
            data_vars=data_vars,
            attrs=md_helper.get_global_attributes(),
        )

        basename = f"{self.output_dir}/milli_psd_{year:04}{month:02}{day:02}"
        nc_filename = f"{basename}.nc"
        save_dataset_to_netcdf(ds_result, nc_filename)
        if self.gen_csv:
            save_csv(aggregated_result, f"{basename}.csv")

        self.file_helper.day_completed()

        return nc_filename

    def process_hours_minutes(self, hour_and_minutes: List[Tuple[int, int]]):
        info(f"Processing {len(hour_and_minutes)} segments ...")
        for at_hour, at_minute in hour_and_minutes:
            self.process_segment_at_hour_minute(at_hour, at_minute)

    def process_segment_at_hour_minute(self, at_hour: int, at_minute: int):
        file_helper = self.file_helper
        year, month, day = file_helper.year, file_helper.month, file_helper.day
        assert year is not None and month is not None and day is not None

        info(f"Segment at {at_hour:02}h:{at_minute:02}m ...")
        info(f"  - extracting {file_helper.segment_size_in_mins * 60}-sec segment:")
        extraction = file_helper.extract_audio_segment(at_hour, at_minute)
        if extraction is None:
            warn(f"cannot get audio segment at {at_hour:02}:{at_minute:02}")
            return

        audio_info, audio_segment = extraction

        if self.pypam_support is None:
            self.pypam_support = PypamSupport(
                audio_info.samplerate, subset_to=self.subset_to
            )
        elif self.pypam_support.fs != audio_info.samplerate:
            info(
                f"ERROR: samplerate changed from {self.pypam_support.fs} to {audio_info.samplerate}"
            )
            return

        if self.save_extracted_wav:
            wav_filename = f"{self.output_dir}/extracted_{year:04}{month:02}{day:02}_{at_hour:02}{at_minute:02}00.wav"
            sf.write(
                wav_filename, audio_segment, audio_info.samplerate, audio_info.subtype
            )
            info(f"  saved extracted wav: {wav_filename} len={len(audio_segment):,}")

        info("  - processing ...")

        if self.voltage_multiplier is not None:
            audio_segment *= self.voltage_multiplier

        dt = datetime(year, month, day, at_hour, at_minute, 0)
        self.pypam_support.add_segment(audio_segment, dt)

        # TODO remove individual segment reports
        if self.save_segment_result:
            iso_minute = f"{year:04}-{month:02}-{day:02}T{at_hour:02}:{at_minute:02}:00Z"
            milli_psd = self.pypam_support.get_milli_psd(
                audio_segment, iso_minute, self.sensitivity_da
            )
            # Note: preliminary naming for output, etc.
            basename = (
                f"milli_psd_{year:04}{month:02}{day:02}_{at_hour:02}{at_minute:02}00"
            )
            save_netcdf(milli_psd, f"{self.output_dir}/{basename}.nc")
            if self.gen_csv:
                save_csv(milli_psd, f"{self.output_dir}/{basename}.csv")
