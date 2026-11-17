/* Line icons at a single 16px grid and 1.5 stroke, drawn to match the type's
 * weight. All are decorative — the control around them carries the label. */

type P = { size?: number }

const wrap = (size: number, children: React.ReactNode) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {children}
  </svg>
)

export const Check = ({ size = 16 }: P) => wrap(size, <path d="M3 8.5l3.5 3.5L13 4.5" />)
export const Cross = ({ size = 16 }: P) => wrap(size, <path d="M4 4l8 8M12 4l-8 8" />)
export const Plus = ({ size = 16 }: P) => wrap(size, <path d="M8 3v10M3 8h10" />)
export const Minus = ({ size = 16 }: P) => wrap(size, <path d="M3 8h10" />)
export const Play = ({ size = 16 }: P) => wrap(size, <path d="M5 3.5l7 4.5-7 4.5z" />)
export const Pause = ({ size = 16 }: P) => wrap(size, <><path d="M6 3.5v9" /><path d="M10 3.5v9" /></>)
export const Trash = ({ size = 16 }: P) =>
  wrap(size, <><path d="M3 4.5h10" /><path d="M6.5 4.5V3h3v1.5" /><path d="M4.5 4.5l.6 8h5.8l.6-8" /></>)
export const Download = ({ size = 16 }: P) =>
  wrap(size, <><path d="M8 2.5v8" /><path d="M4.5 7.5L8 11l3.5-3.5" /><path d="M3 13h10" /></>)
export const Upload = ({ size = 16 }: P) =>
  wrap(size, <><path d="M8 11V3" /><path d="M4.5 6L8 2.5 11.5 6" /><path d="M3 13h10" /></>)
export const ChevronRight = ({ size = 16 }: P) => wrap(size, <path d="M6 3.5L10.5 8 6 12.5" />)
export const ChevronLeft = ({ size = 16 }: P) => wrap(size, <path d="M10 3.5L5.5 8 10 12.5" />)
export const ChevronDown = ({ size = 16 }: P) => wrap(size, <path d="M3.5 6L8 10.5 12.5 6" />)
export const Refresh = ({ size = 16 }: P) =>
  wrap(size, <><path d="M13 8a5 5 0 1 1-1.6-3.7" /><path d="M13 2.5V5h-2.5" /></>)
export const Send = ({ size = 16 }: P) => wrap(size, <><path d="M14 2L7 9" /><path d="M14 2l-4.5 12-2.5-5-5-2.5L14 2z" /></>)
export const Alert = ({ size = 16 }: P) =>
  wrap(size, <><path d="M8 2.5l6 11H2l6-11z" /><path d="M8 6.5v3" /><path d="M8 11.6v.01" /></>)
export const Clock = ({ size = 16 }: P) => wrap(size, <><circle cx="8" cy="8" r="5.5" /><path d="M8 5v3.2l2 1.3" /></>)
export const Doc = ({ size = 16 }: P) =>
  wrap(size, <><path d="M9 2H4v12h8V5L9 2z" /><path d="M9 2v3h3" /></>)
export const Sheet = ({ size = 16 }: P) =>
  wrap(size, <><rect x="2.5" y="2.5" width="11" height="11" rx="1" /><path d="M2.5 6.5h11M2.5 10h11M6.5 2.5v11" /></>)
export const Picture = ({ size = 16 }: P) =>
  wrap(size, <><rect x="2.5" y="3" width="11" height="10" rx="1" /><circle cx="6" cy="6.5" r="1.2" /><path d="M3 12l3.5-3.5 2.5 2.5 2-2 2.5 2.5" /></>)
export const People = ({ size = 16 }: P) =>
  wrap(size, <><circle cx="6" cy="6" r="2.5" /><path d="M2 13.5c0-2.2 1.8-3.5 4-3.5s4 1.3 4 3.5" /><path d="M11 4.2a2.5 2.5 0 0 1 0 4.6" /><path d="M12 10.3c1.3.5 2 1.6 2 3.2" /></>)
export const Star = ({ size = 16 }: P) => wrap(size, <path d="M8 2l1.8 3.8 4.2.5-3 2.9.8 4.1L8 11.4 4.2 13.3l.8-4.1-3-2.9 4.2-.5L8 2z" />)
export const Link = ({ size = 16 }: P) =>
  wrap(size, <><path d="M6.5 9.5a2.8 2.8 0 0 0 4 0l2-2a2.8 2.8 0 0 0-4-4l-.8.8" /><path d="M9.5 6.5a2.8 2.8 0 0 0-4 0l-2 2a2.8 2.8 0 0 0 4 4l.8-.8" /></>)
export const Copy = ({ size = 16 }: P) =>
  wrap(size, <><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" /><path d="M10.5 3.5H4a1.5 1.5 0 0 0-1.5 1.5v6.5" /></>)
export const Search = ({ size = 16 }: P) => wrap(size, <><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5L14 14" /></>)
export const Spark = ({ size = 16 }: P) =>
  wrap(size, <><path d="M8 2v3M8 11v3M2 8h3M11 8h3" /><circle cx="8" cy="8" r="2" /></>)
