/**
 * useSTT — L79 accessibility bonus (browser-native STT).
 *
 * Wraps the Web Speech API SpeechRecognition (or webkitSpeechRecognition
 * on Safari/Chrome) as a small React-friendly hook. Returns:
 *   - listening: boolean       — currently capturing audio
 *   - start(onTranscript)      — begin recording; calls back with each
 *                                interim+final transcript chunk so the
 *                                UI can stream text into the input box
 *   - stop()                   — manually stop capture
 *   - supported: boolean       — feature-detect (hide UI when false)
 *
 * Per L79: no auto-send on stop. Caller decides when to submit. The
 * student can edit the transcribed text before pressing send.
 *
 * Browser support: Chrome / Edge / Safari (via webkitSpeechRecognition).
 * Firefox no support — `supported` is false there.
 */
import { useCallback, useEffect, useRef, useState } from "react";

// Web Speech API typings — minimal local declarations to avoid pulling
// the full TS dom-speech-api package as a dependency.
interface SpeechRecognitionAlternative {
  transcript: string;
  confidence: number;
}
interface SpeechRecognitionResult {
  isFinal: boolean;
  readonly length: number;
  [index: number]: SpeechRecognitionAlternative;
}
interface SpeechRecognitionResultList {
  readonly length: number;
  [index: number]: SpeechRecognitionResult;
}
interface SpeechRecognitionEvent {
  results: SpeechRecognitionResultList;
  resultIndex: number;
}
interface SpeechRecognitionInstance {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: SpeechRecognitionEvent) => void) | null;
  onerror: ((e: Event) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}
interface SpeechRecognitionCtor {
  new (): SpeechRecognitionInstance;
}

function getRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface STTApi {
  listening: boolean;
  supported: boolean;
  start: (onTranscript: (text: string, isFinal: boolean) => void) => void;
  stop: () => void;
}

export function useSTT(): STTApi {
  const ctor = getRecognitionCtor();
  const supported = ctor !== null;
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);
  const [listening, setListening] = useState(false);
  // Cache the latest onTranscript so the .onresult handler captures the
  // current closure — React state changes shouldn't lose callbacks.
  const onTranscriptRef = useRef<((text: string, isFinal: boolean) => void) | null>(null);

  // Stop capture cleanly on unmount.
  useEffect(() => {
    return () => {
      try {
        recognitionRef.current?.abort();
      } catch {
        // ignore — abort can throw if recognition was never started
      }
    };
  }, []);

  const start = useCallback(
    (onTranscript: (text: string, isFinal: boolean) => void) => {
      if (!ctor) return;
      onTranscriptRef.current = onTranscript;
      try {
        // If a previous recognition is still alive, abort it first.
        if (recognitionRef.current) {
          recognitionRef.current.abort();
        }
        const rec = new ctor();
        rec.continuous = false; // single-utterance per L79 (click-to-record)
        rec.interimResults = true;
        rec.lang = "en-US";
        rec.onresult = (e: SpeechRecognitionEvent) => {
          const results = e.results;
          // Concatenate all results since the last reset for a stable
          // running transcript (interim results overwrite each render).
          let combined = "";
          let isFinal = true;
          for (let i = 0; i < results.length; i++) {
            const r = results[i];
            if (r.length > 0) combined += r[0].transcript;
            if (!r.isFinal) isFinal = false;
          }
          onTranscriptRef.current?.(combined.trim(), isFinal);
        };
        rec.onerror = () => {
          setListening(false);
        };
        rec.onend = () => {
          setListening(false);
        };
        rec.start();
        recognitionRef.current = rec;
        setListening(true);
      } catch {
        setListening(false);
      }
    },
    [ctor],
  );

  const stop = useCallback(() => {
    try {
      recognitionRef.current?.stop();
    } catch {
      // ignore — stop on a never-started recognition can throw
    }
    setListening(false);
  }, []);

  return { listening, supported, start, stop };
}
