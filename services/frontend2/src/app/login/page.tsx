"use client";

import { signIn } from "next-auth/react";
import type { ReactElement } from "react";
import styles from "./login.module.css";

interface Ring {
  rx: number; ry: number; tilt: number;
  dur: number; color: string; count: number; size: number; opacity: number;
}

function NetworkGraph({ p = "a" }: { p?: string }): ReactElement {
  const cx = 410, cy = 145;
  const W = 860, H = 290;

  const rings: Ring[] = [
    { rx: 110, ry: 32, tilt: -8,  dur: 10, color: "#818cf8", count: 2, size: 14, opacity: 1    },
    { rx: 200, ry: 56, tilt: -11, dur: 18, color: "#4361ee", count: 3, size: 15, opacity: 0.80 },
    { rx: 320, ry: 78, tilt: -14, dur: 30, color: "#60a5fa", count: 4, size: 12, opacity: 0.50 },
  ];

  const stars: [number, number, number][] = [
    [80,28,1.2],[580,22,1.5],[720,95,1],[660,248,1],[110,245,1],
    [40,185,1.4],[360,18,1],[620,142,1.2],[88,148,1],[430,24,1],
  ];

  // 3 ghost steps only, no blur filter (blur is very expensive)
  const ghostSteps = [
    { n: 1, scale: 0.65, opacity: 0.35 },
    { n: 2, scale: 0.38, opacity: 0.15 },
  ];

  const renderNodes = (ring: Ring, ri: number, maskId: string): ReactElement => {
    const path = `M ${ring.rx},0 A ${ring.rx},${ring.ry} 0 1,1 -${ring.rx},0 A ${ring.rx},${ring.ry} 0 1,1 ${ring.rx},0`;
    const step = ring.dur / 60;
    return (
      <g key={`${maskId}-${ri}`} mask={`url(#${maskId})`}>
        <g transform={`rotate(${ring.tilt}, ${cx}, ${cy})`} opacity={ring.opacity}>
          {Array.from({ length: ring.count }).map((_, ni) => {
            const base = (ni * ring.dur) / ring.count;
            return (
              <g key={ni}>
                {/* Ghost trail — no filter for performance */}
                {ghostSteps.map((g) => {
                  const raw = ((base - step * g.n) % ring.dur + ring.dur) % ring.dur;
                  return (
                    <g key={g.n} transform={`translate(${cx}, ${cy})`}>
                      <circle r={ring.size * g.scale} fill={ring.color} opacity={g.opacity}/>
                      <animateMotion dur={`${ring.dur}s`} begin={`-${raw.toFixed(3)}s`}
                        repeatCount="indefinite" path={path}/>
                    </g>
                  );
                })}
                {/* Main head — person profile */}
                <g transform={`translate(${cx}, ${cy})`}>
                  <circle r={ring.size * 1.8} fill={ring.color} opacity="0.12"/>
                  <circle r={ring.size} fill={ring.color}/>
                  <circle cx={0} cy={-ring.size * 0.2} r={ring.size * 0.28} fill="#fff"/>
                  <path
                    d={`M${-ring.size*0.44} ${ring.size*0.28} Q${-ring.size*0.44} ${ring.size*0.06} 0 ${ring.size*0.06} Q${ring.size*0.44} ${ring.size*0.06} ${ring.size*0.44} ${ring.size*0.28}`}
                    fill="#fff"
                  />
                  <animateMotion dur={`${ring.dur}s`} begin={`-${base.toFixed(3)}s`}
                    repeatCount="indefinite" path={path}/>
                </g>
              </g>
            );
          })}
        </g>
      </g>
    );
  };

  return (
    <svg
      className={styles.illustrationBg}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <radialGradient id={`${p}coreGlow`} cx="50%" cy="50%" r="50%">
          <stop offset="0%"   stopColor="#a5b4fc" stopOpacity="0.35"/>
          <stop offset="100%" stopColor="#4361ee" stopOpacity="0"/>
        </radialGradient>
        <radialGradient id={`${p}cg`} cx="30%" cy="28%" r="72%">
          <stop offset="0%"   stopColor="#6c8ef7"/>
          <stop offset="55%"  stopColor="#4361ee"/>
          <stop offset="100%" stopColor="#2d4fd6"/>
        </radialGradient>
        <filter id={`${p}centerGlow`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="6" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        {/* Fade zone around cy so back/front transition is smooth, not a hard cut */}
        <linearGradient id={`${p}backFade`} x1="0" y1={cy - 36} x2="0" y2={cy + 36}
          gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stopColor="white"/>
          <stop offset="100%" stopColor="black"/>
        </linearGradient>
        <linearGradient id={`${p}frontFade`} x1="0" y1={cy - 36} x2="0" y2={cy + 36}
          gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stopColor="black"/>
          <stop offset="100%" stopColor="white"/>
        </linearGradient>
        <mask id={`${p}maskBack`}>
          <rect x="0" y="0"        width={W} height={cy - 36} fill="white"/>
          <rect x="0" y={cy - 36}  width={W} height={72}      fill={`url(#${p}backFade)`}/>
        </mask>
        <mask id={`${p}maskFront`}>
          <rect x="0" y={cy - 36}  width={W} height={72}      fill={`url(#${p}frontFade)`}/>
          <rect x="0" y={cy + 36}  width={W} height={H}       fill="white"/>
        </mask>
      </defs>

      {stars.map(([sx, sy, sr], i) => (
        <circle key={i} cx={sx} cy={sy} r={sr} fill="#818cf8" opacity="0.5"/>
      ))}

      <ellipse cx={cx} cy={cy} rx="240" ry="90"
        fill={`url(#${p}coreGlow)`} transform={`rotate(-12, ${cx}, ${cy})`}/>

      {/* Rings + nodes BACK */}
      {rings.map((ring, ri) => (
        <g key={`ring-back-${ri}`} mask={`url(#${p}maskBack)`}>
          <g transform={`rotate(${ring.tilt}, ${cx}, ${cy})`} opacity={ring.opacity}>
            <ellipse cx={cx} cy={cy} rx={ring.rx} ry={ring.ry}
              stroke={ring.color} strokeWidth="1.5" strokeDasharray="4 5" fill="none" opacity="0.75"/>
          </g>
        </g>
      ))}
      {rings.map((ring, ri) => renderNodes(ring, ri, `${p}maskBack`))}

      {/* Center */}
      <circle cx={cx} cy={cy} r="62" fill="#4361ee" opacity="0.05">
        <animate attributeName="r" values="62;78;62" dur="3s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.05;0.01;0.05" dur="3s" repeatCount="indefinite"/>
      </circle>
      <circle cx={cx} cy={cy} r="46" fill="#4361ee" opacity="0.09">
        <animate attributeName="r" values="46;54;46" dur="3s" repeatCount="indefinite"/>
      </circle>
      <circle cx={cx} cy={cy} r="58" fill="none" stroke="#818cf8" strokeWidth="1" opacity="0.4">
        <animate attributeName="r" values="58;68;58" dur="3s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.4;0.1;0.4" dur="3s" repeatCount="indefinite"/>
      </circle>
      <circle cx={cx} cy={cy} r="48" fill="none" stroke="#a5b4fc" strokeWidth="1.5" opacity="0.5">
        <animate attributeName="r" values="48;54;48" dur="3s" repeatCount="indefinite"/>
      </circle>
      <circle cx={cx} cy={cy} r="42" fill={`url(#${p}cg)`} filter={`url(#${p}centerGlow)`}/>
      <circle cx={cx - 12} cy={cy - 12} r="11" fill="#fff" opacity="0.15"/>
      <text x={cx} y={cy + 9} textAnchor="middle"
        fontSize="26" fontWeight="900" fill="#fff"
        fontFamily="-apple-system, BlinkMacSystemFont, sans-serif" letterSpacing="-1"
      >H</text>

      {/* Rings + nodes FRONT */}
      {rings.map((ring, ri) => renderNodes(ring, ri, `${p}maskFront`))}
      {rings.map((ring, ri) => (
        <g key={`ring-front-${ri}`} mask={`url(#${p}maskFront)`}>
          <g transform={`rotate(${ring.tilt}, ${cx}, ${cy})`} opacity={ring.opacity}>
            <ellipse cx={cx} cy={cy} rx={ring.rx} ry={ring.ry}
              stroke={ring.color} strokeWidth="1.5" strokeDasharray="4 5" fill="none" opacity="0.75"/>
          </g>
        </g>
      ))}
    </svg>
  );
}

export default function LoginPage(): ReactElement {
  return (
    <div className={styles.page}>

      {/* Mobile background illustration */}
      <div className={styles.mobileIllustration} aria-hidden="true">
        <NetworkGraph p="m" />
      </div>

      {/* Content */}
      <div className={styles.content}>

        {/* Left — desktop only */}
        <div className={styles.left}>
          <h1 className={styles.headline}>
            One identity,<br />
            <em>infinite</em> clarity.
          </h1>
          <p className={styles.subheadline}>
            Resolve the same real-world person across POS, CRM, and third-party systems — with precision-first matching.
          </p>
          <NetworkGraph p="d" />
        </div>

        {/* Right — floating card */}
        <div className={styles.card}>
          <div className={styles.cardLogo}>
            <div className={styles.logoMark}>H</div>
            <div>
              <span className={styles.logoText}>Hyper<span>P</span></span>
              <p className={styles.logoSub}>Profile Unifier</p>
            </div>
          </div>
          <p className={styles.formEyebrow}>Secure access</p>
          <h2 className={styles.formTitle}>Welcome back</h2>
          <p className={styles.formSubtitle}>
            Sign in with your organisation Google account to continue.
          </p>

          <button
            className={styles.signInBtn}
            onClick={() => { void signIn("google", { callbackUrl: "/" }); }}
          >
            <svg className={styles.googleIcon} viewBox="0 0 24 24">
              <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#fff"/>
              <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#fff"/>
              <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#fff"/>
              <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#fff"/>
            </svg>
            Continue with Google
          </button>

          <p className={styles.notice}>
            Access requires administrator approval. New accounts are reviewed before activation.
          </p>
        </div>

      </div>
    </div>
  );
}
