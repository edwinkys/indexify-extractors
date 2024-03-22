from typing import List, Optional

from indexify_extractor_sdk import (
    Extractor,
    Content,
)
import torch
from pydantic import BaseModel
from pydub import AudioSegment
import os
from nemo.collections.asr.models.msdd_models import NeuralDiarizer
from helpers import wav2vec2_langs, filter_missing_timestamps, create_config
import whisperx
from helpers import get_words_speaker_mapping, punct_model_langs
from deepmultilingualpunctuation import PunctuationModel
import re
from helpers import (
    get_realigned_ws_mapping_with_punctuation,
    get_sentences_speaker_mapping,
)
import tempfile
import mimetypes
import json


class InputParams(BaseModel):
    language: Optional[str] = None
    stemming: bool = True
    batch_size: int = 0
    model: str = "distil-medium.en"
    supress_numerals: bool = False
    device: str = "cuda" if torch.cuda.is_available() else "cpu"


class WhisperDiarizationExtractor(Extractor):
    name = "tensorlake/whisper-diarization"
    description = "Whisper ASR"
    system_dependencies = ["ffmpeg"]
    input_mime_types = ["audio", "audio/mpeg"]

    def __init__(self):
        super().__init__()

    def get_vocal_target(self, params: InputParams, file_path: str):
        if params.stemming:
            # Isolate vocals from the rest of the audio

            return_code = os.system(
                f'python3 -m demucs.separate -n htdemucs --two-stems=vocals "{file_path}" -o "temp_outputs"'
            )

            if return_code != 0:
                print(
                    "Source splitting failed, using original audio file. Use --no-stem argument to disable it."
                )
                vocal_target = file_path
            else:
                vocal_target = os.path.join(
                    "temp_outputs",
                    "htdemucs",
                    os.path.splitext(os.path.basename(file_path))[0],
                    "vocals.wav",
                )
        else:
            vocal_target = file_path
        return vocal_target

    def extract(self, content: Content, params: InputParams) -> List[Content]:
        suffix = mimetypes.guess_extension(content.content_type)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as inputtmpfile:
            inputtmpfile.write(content.data)
            inputtmpfile.flush()
            vocal_target = self.get_vocal_target(params, file_path=inputtmpfile.name)
            whisper_results, language = self.transcribe(vocal_target, params)
            word_timestamps = self.get_word_timestamps(
                whisper_results, language, vocal_target, params
            )

            # convert audio to mono for NeMo compatibility
            sound = AudioSegment.from_file(vocal_target).set_channels(1)
            ROOT = os.getcwd()
            temp_path = os.path.join(ROOT, "temp_outputs")
            os.makedirs(temp_path, exist_ok=True)
            sound.export(os.path.join(temp_path, "mono_file.wav"), format="wav")

            # Initialize NeMo MSDD diarization model
            msdd_model = NeuralDiarizer(cfg=create_config(temp_path)).to(params.device)
            msdd_model.diarize()

            del msdd_model
            torch.cuda.empty_cache()

            speaker_ts = []
            with open(
                os.path.join(temp_path, "pred_rttms", "mono_file.rttm"), "r"
            ) as f:
                lines = f.readlines()
                for line in lines:
                    line_list = line.split(" ")
                    s = int(float(line_list[5]) * 1000)
                    e = s + int(float(line_list[8]) * 1000)
                    speaker_ts.append([s, e, int(line_list[11].split("_")[-1])])

            wsm = get_words_speaker_mapping(word_timestamps, speaker_ts, "start")

            if language in punct_model_langs:
                # restoring punctuation in the transcript to help realign the sentences
                punct_model = PunctuationModel(model="kredor/punctuate-all")

                words_list = list(map(lambda x: x["word"], wsm))

                labled_words = punct_model.predict(words_list)

                ending_puncts = ".?!"
                model_puncts = ".,;:!?"

                # We don't want to punctuate U.S.A. with a period. Right?
                is_acronym = lambda x: re.fullmatch(r"\b(?:[a-zA-Z]\.){2,}", x)

                for word_dict, labeled_tuple in zip(wsm, labled_words):
                    word = word_dict["word"]
                    if (
                        word
                        and labeled_tuple[1] in ending_puncts
                        and (word[-1] not in model_puncts or is_acronym(word))
                    ):
                        word += labeled_tuple[1]
                        if word.endswith(".."):
                            word = word.rstrip(".")
                        word_dict["word"] = word

            else:
                print(
                    f"Punctuation restoration is not available for {language} language. Using the original punctuation."
                )

            wsm = get_realigned_ws_mapping_with_punctuation(wsm)
            ssm = get_sentences_speaker_mapping(wsm, speaker_ts)
            print(ssm)
            return [
                Content(
                    content_type="application/json",
                    data=bytes(json.dumps(ssm), "utf-8"),
                )
            ]

    def get_word_timestamps(self, whisper_results, language, vocal_target, params):
        if language in wav2vec2_langs:
            alignment_model, metadata = whisperx.load_align_model(
                language_code=language, device=params.device
            )
            result_aligned = whisperx.align(
                whisper_results,
                alignment_model,
                metadata,
                vocal_target,
                params.device,
            )
            word_timestamps = filter_missing_timestamps(
                result_aligned["word_segments"],
                initial_timestamp=whisper_results[0].get("start"),
                final_timestamp=whisper_results[-1].get("end"),
            )
            # clear gpu vram
            del alignment_model
            torch.cuda.empty_cache()
        else:
            assert (
                params.batch_size
                == 0  # TODO: add a better check for word timestamps existence
            ), (
                f"Unsupported language: {language}, use --batch_size to 0"
                " to generate word timestamps using whisper directly and fix this error."
            )
            word_timestamps = []
            for segment in whisper_results:
                for word in segment["words"]:
                    word_timestamps.append(
                        {"word": word[2], "start": word[0], "end": word[1]}
                    )
        return word_timestamps

    def transcribe(self, vocal_target, params: InputParams):
        mtypes = {"cpu": "int8", "cuda": "float16"}
        # transcribe
        if params.batch_size != 0:
            from transcription_helpers import transcribe_batched

            whisper_results, language = transcribe_batched(
                vocal_target,
                params.language,
                params.batch_size,
                params.model,
                mtypes[params.device],
                params.supress_numerals,
                params.device,
            )
        else:
            from transcription_helpers import transcribe

            whisper_results, language = transcribe(
                vocal_target,
                params.language,
                params.model,
                mtypes[params.device],
                params.supress_numerals,
                params.device,
            )
        return whisper_results, language

    def sample_input(self) -> Content:
        return self.sample_mp3()


if __name__ == "__main__":
    contents = WhisperDiarizationExtractor().extract_sample_input()
    print(len(contents))
    for content in contents:
        print(len(content.features))
        for feature in content.features:
            print(feature.value)
