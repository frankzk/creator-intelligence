import React, {useMemo} from 'react';
import {
  AbsoluteFill,
  OffthreadVideo,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

export type Word = {text: string; start: number; end: number};

export type ModuleProps = {
  srcName: string; // relativo a public/, ej. "inputs/H1.mp4"
  durationInSeconds: number;
  words: Word[];
  overlayText: string;
  moduleType: 'hook' | 'body' | 'cta';
};

export const defaultModuleProps: ModuleProps = {
  srcName: '',
  durationInSeconds: 5,
  words: [],
  overlayText: 'Texto de ejemplo',
  moduleType: 'hook',
};

// Zonas seguras TikTok: la UI tapa ~el 15% inferior y el borde derecho.
// Captions al 60% de altura; texto overlay arriba al 12%.
const CAPTION_TOP = '60%';
const OVERLAY_TOP = '12%';

type Page = {start: number; end: number; words: Word[]};

// Páginas estilo karaoke: máx 3 palabras, corta en silencios largos.
const buildPages = (words: Word[]): Page[] => {
  const pages: Page[] = [];
  let current: Word[] = [];
  for (const w of words) {
    const last = current[current.length - 1];
    if (current.length >= 3 || (last && w.start - last.end > 0.8)) {
      pages.push({start: current[0].start, end: current[current.length - 1].end, words: current});
      current = [];
    }
    current.push(w);
  }
  if (current.length > 0) {
    pages.push({start: current[0].start, end: current[current.length - 1].end, words: current});
  }
  return pages;
};

const strokeStyle: React.CSSProperties = {
  WebkitTextStroke: '10px #000',
  paintOrder: 'stroke fill',
  textShadow: '0 4px 16px rgba(0,0,0,0.45)',
};

const Captions: React.FC<{words: Word[]}> = ({words}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = frame / fps;
  const pages = useMemo(() => buildPages(words), [words]);

  const idx = pages.findIndex((p, i) => {
    const next = pages[i + 1];
    return t >= p.start && t < (next ? next.start : p.end + 0.35);
  });
  if (idx === -1) return null;
  const page = pages[idx];

  const pop = spring({frame: frame - Math.round(page.start * fps), fps, config: {damping: 200}, durationInFrames: 6});

  return (
    <AbsoluteFill style={{justifyContent: 'flex-start', alignItems: 'center'}}>
      <div
        style={{
          position: 'absolute',
          top: CAPTION_TOP,
          width: '86%',
          textAlign: 'center',
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif',
          fontWeight: 800,
          fontSize: 64,
          lineHeight: 1.2,
          color: '#fff',
          transform: `scale(${0.92 + pop * 0.08})`,
          ...strokeStyle,
        }}
      >
        {page.words.map((w, i) => {
          const active = t >= w.start && t < w.end + 0.05;
          return (
            <span
              key={i}
              style={{
                color: active ? '#FDE047' : '#fff',
                display: 'inline-block',
                transform: active ? 'scale(1.08)' : 'scale(1)',
                margin: '0 10px',
              }}
            >
              {w.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const OverlayText: React.FC<{text: string; moduleType: ModuleProps['moduleType']}> = ({text, moduleType}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();

  // Hooks/cuerpos: visible los primeros 3s. CTA: visible todo el módulo (urgencia).
  const visibleFrames = moduleType === 'cta' ? durationInFrames : Math.min(3 * fps, durationInFrames);
  if (frame > visibleFrames) return null;

  const pop = spring({frame, fps, config: {damping: 14, stiffness: 160}, durationInFrames: 14});
  const fadeOut =
    moduleType === 'cta'
      ? 1
      : interpolate(frame, [visibleFrames - 8, visibleFrames], [1, 0], {
          extrapolateLeft: 'clamp',
          extrapolateRight: 'clamp',
        });

  return (
    <AbsoluteFill style={{alignItems: 'center'}}>
      <div
        style={{
          position: 'absolute',
          top: OVERLAY_TOP,
          width: '84%',
          textAlign: 'center',
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif',
          fontWeight: 900,
          fontSize: 72,
          lineHeight: 1.15,
          color: '#fff',
          opacity: fadeOut,
          transform: `scale(${0.6 + pop * 0.4})`,
          ...strokeStyle,
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

export const ModuleVideo: React.FC<ModuleProps> = ({srcName, words, overlayText, moduleType}) => {
  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      {srcName ? (
        <OffthreadVideo src={staticFile(srcName)} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
      ) : (
        // Preview en Studio sin video cargado
        <AbsoluteFill style={{background: 'linear-gradient(180deg,#1e1b4b,#0f172a)'}} />
      )}
      {words.length > 0 && <Captions words={words} />}
      {overlayText ? <OverlayText text={overlayText} moduleType={moduleType} /> : null}
    </AbsoluteFill>
  );
};
