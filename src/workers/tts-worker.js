import { PiperTTS, TextSplitterStream } from "../lib/piper-tts.js";
import { resumeStartIndex } from "../utils/resume-match.js";

let tts = null;
let run = 0;

// Initialize the model
async function initializeModel(modelName = null) {
  try {
    // Default to the original model if no model name provided
    const defaultModel = 'en_US-libritts_r-medium';
    const model = modelName || defaultModel;
    // Encode to handle spaces or special characters in filenames
    const encodedModel = encodeURIComponent(model);
    
    // Construct paths - use API endpoint to fetch from R2
    const modelPath = `/api/model/${encodedModel}.onnx`;
    const configPath = `/api/model/${encodedModel}.onnx.json`;
    
    tts = await PiperTTS.from_pretrained(modelPath, configPath);
    
    // Get available speakers
    const speakers = tts.getSpeakers();
    
    self.postMessage({ status: "ready", voices: speakers });
  } catch (e) {
    console.error("Error loading model:", e);
    self.postMessage({ status: "error", data: e.message });
  }
}

// Handle voice preview
async function handlePreview(text, voice, speed) {
  try {
    const streamer = new TextSplitterStream();
    await streamer.push(text);
    streamer.close();

    const speakerId = typeof voice === 'number' ? voice : parseInt(voice) || 0;
    const lengthScale = 1.0 / (speed || 1.0);
    
    const stream = tts.stream(streamer, { 
      speakerId, 
      lengthScale
    });

    // Get just the first chunk for preview
    for await (const { audio } of stream) {
      // Create and play preview audio
      const audioBlob = audio.toBlob();
      self.postMessage({ status: "preview", audio: audioBlob });
      break; // Only preview the first chunk
    }
  } catch (error) {
    console.error('Error generating preview:', error);
  }
}

// Listen for messages from the main thread
self.addEventListener("message", async (e) => {
  const { type, text, voice, speed, model, token, resume } = e.data;
  
  // Handle initialization
  if (type === 'init') {
    await initializeModel(model);
    return;
  }

  if (type === 'stop') {
    run += 1;
    return;
  }
  
  // Handle TTS generation
  if (!tts) {
    self.postMessage({ status: "error", data: "Model not initialized" });
    return;
  }
  
  // Handle voice preview
  if (type === 'preview') {
    await handlePreview(text, voice, speed);
    return;
  }

  const current = ++run;
  const streamer = new TextSplitterStream();

  await streamer.push(text);
  if (current !== run) return;
  // Nghe tiếp phát từ câu đang dở. Câu phía trước ghép vào WAV sau, khi người nghe đã có tiếng.
  const sentences = streamer.chunks.slice();
  const start = resumeStartIndex(sentences, resume);
  streamer.chunks = sentences.slice(start);
  streamer.close(); // Indicate we won't add more text

  // Convert voice from voice ID to speaker ID
  const speakerId = typeof voice === 'number' ? voice : parseInt(voice) || 0;
  
  // console.log('🎤 Worker received voice ID:', voice);
  // console.log('🎤 Worker converted to speaker ID:', speakerId);
  
  // Convert speed to lengthScale (inverse relationship: higher speed = lower lengthScale)
  const lengthScale = 1.0 / (speed || 1.0);
  
  const stream = tts.stream(streamer, { 
    speakerId, 
    lengthScale
  });
  const chunks = [];
  let sentenceIndex = start;

  try {
    for await (const { text: sentence, audio } of stream) {
      if (current !== run) return;
      self.postMessage({
        status: "stream",
        token,
        index: sentenceIndex,
        chunk: {
          audio: audio.toBlob(),
          text: sentence,
        },
      });
      sentenceIndex += 1;
      chunks.push(audio);
    }
  } catch (error) {
    if (current !== run) return;
    console.error("Error during streaming:", error);
    self.postMessage({ status: "error", data: error.message, token });
    return;
  }

  if (current !== run) return;
  self.postMessage({ status: "playback-done", token });

  const prefixChunks = [];
  if (start > 0) {
    const prefix = new TextSplitterStream();
    prefix.chunks = sentences.slice(0, start);
    prefix.close();
    try {
      for await (const { audio } of tts.stream(prefix, { speakerId, lengthScale })) {
        if (current !== run) return;
        prefixChunks.push(audio);
      }
    } catch (error) {
      if (current !== run) return;
      console.error("Error during prefix:", error);
      self.postMessage({ status: "error", data: error.message, token });
      return;
    }
  }

  if (current !== run) return;
  let audio;
  try {
    audio = mergeChunks([...prefixChunks, ...chunks]);
  } catch (error) {
    if (current !== run) return;
    console.error("Error processing audio chunks:", error);
    self.postMessage({ status: "error", data: error.message, token });
    return;
  }

  if (current !== run) return;
  self.postMessage({ status: "complete", token, audio: audio?.toBlob() });
});

function mergeChunks(chunks) {
  if (!chunks.length) return null;
  const originalSamplingRate = chunks[0].sampling_rate;
  const length = chunks.reduce((sum, chunk) => sum + chunk.audio.length, 0);
  const waveform = new Float32Array(length);
  let offset = 0;
  for (const { audio } of chunks) {
    waveform.set(audio, offset);
    offset += audio.length;
  }
  normalizePeak(waveform, 1.0);
  return new chunks[0].constructor(waveform, originalSamplingRate);
}

function normalizePeak(f32, target = 0.9) {
  if (!f32?.length) return;
  let max = 1e-9;
  for (let i = 0; i < f32.length; i++) max = Math.max(max, Math.abs(f32[i]));
  const g = Math.min(4, target / max);
  if (g < 1) {
    for (let i = 0; i < f32.length; i++) f32[i] *= g;
  }
}

// Note: Initialization now handled via init message from UI
