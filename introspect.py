"""Print the google-genai SDK surface this harness relies on (no network, no key)."""

import inspect
from importlib.metadata import version

from google.genai import live, types


def fields(model) -> list[str]:
    return sorted(model.model_fields.keys())


def show(title: str, names) -> None:
    print(f"\n== {title} ==")
    print(", ".join(names))


print("google-genai", version("google-genai"))

show("types.LiveConnectConfig fields", fields(types.LiveConnectConfig))
show("types.FunctionDeclaration fields", fields(types.FunctionDeclaration))
print("FunctionDeclaration.behavior annotation:",
      types.FunctionDeclaration.model_fields["behavior"].annotation
      if "behavior" in types.FunctionDeclaration.model_fields else "MISSING")
if hasattr(types, "Behavior"):
    show("types.Behavior values", [m.value for m in types.Behavior])
show("types.FunctionResponse fields", fields(types.FunctionResponse))
if hasattr(types, "FunctionResponseScheduling"):
    show("types.FunctionResponseScheduling values",
         [m.value for m in types.FunctionResponseScheduling])
show("types.LiveServerMessage fields", fields(types.LiveServerMessage))
show("types.LiveServerContent fields", fields(types.LiveServerContent))
show("types.LiveServerToolCall fields", fields(types.LiveServerToolCall))
show("types.LiveServerToolCallCancellation fields",
     fields(types.LiveServerToolCallCancellation))
show("types.Modality values", [m.value for m in types.Modality])
if hasattr(types, "AudioTranscriptionConfig"):
    show("types.AudioTranscriptionConfig fields", fields(types.AudioTranscriptionConfig))

# --input audio
show("types.Blob fields", fields(types.Blob))
print("Blob.data annotation:", types.Blob.model_fields["data"].annotation)
show("types.RealtimeInputConfig fields", fields(types.RealtimeInputConfig))
show("types.AutomaticActivityDetection fields", fields(types.AutomaticActivityDetection))
show("types.ActivityHandling values", [m.value for m in types.ActivityHandling])
show("types.VoiceActivity fields", fields(types.VoiceActivity))
show("types.VoiceActivityType values", [m.value for m in types.VoiceActivityType])
show("types.VoiceActivityDetectionSignal fields", fields(types.VoiceActivityDetectionSignal))
show("types.VadSignalType values", [m.value for m in types.VadSignalType])

print("\n== AsyncSession signatures ==")
for name in ("send_realtime_input", "send_client_content", "send_tool_response",
             "receive", "close"):
    fn = getattr(live.AsyncSession, name, None)
    print(f"{name}{inspect.signature(fn)}" if fn else f"{name}: MISSING")
print("AsyncLive.connect", inspect.signature(live.AsyncLive.connect))
