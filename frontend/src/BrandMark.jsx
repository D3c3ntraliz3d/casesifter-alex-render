/** CaseSifter brand seal — circular institutional mark (navy / teal + serif CS). */
export function BrandMark({ className = "seal", size, title = "CaseSifter", decorative = false }) {
  const style = size ? { width: size, height: size } : undefined;
  return (
    <svg
      className={className}
      style={style}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      role={decorative ? "presentation" : "img"}
      aria-hidden={decorative ? true : undefined}
      aria-label={decorative ? undefined : title}
    >
      {!decorative ? <title>{title}</title> : null}
      <circle cx="32" cy="32" r="32" fill="#122F47" />
      <circle cx="32" cy="32" r="29.75" fill="none" stroke="#2B5572" strokeWidth="1" />
      <circle cx="32" cy="32" r="27" fill="none" stroke="#0D7372" strokeWidth="1.25" />
      <path
        d="M22 44.5h20M24.5 47h15M27 49.5h10"
        fill="none"
        stroke="#0D7372"
        strokeWidth="1.05"
        strokeLinecap="round"
        opacity="0.55"
      />
      <text
        x="32"
        y="37"
        textAnchor="middle"
        fontFamily="Georgia, 'Times New Roman', Times, serif"
        fontSize="21"
        fontWeight="700"
        fill="#F4FFFE"
        letterSpacing="0.02em"
      >
        CS
      </text>
    </svg>
  );
}

export default BrandMark;
