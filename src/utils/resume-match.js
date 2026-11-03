export function textMatches(text, anchor) {
  const left = String(text || '').replace(/\s+/g, ' ').trim();
  const right = String(anchor || '').replace(/\s+/g, ' ').trim();
  if (!left || !right) return false;
  if (left === right) return true;
  if (right.length < 12) return false;
  return left.startsWith(right) || right.startsWith(left);
}

// Câu cần phát khi nghe tiếp. Anchor thắng vì chỉ số câu lệch sau khi sửa chữ.
export function resumeStartIndex(sentences, resume) {
  if (!resume || !Array.isArray(sentences) || sentences.length === 0) return 0;
  const anchor = resume.anchor || '';
  if (anchor) {
    const found = sentences.findIndex((sentence) => textMatches(sentence, anchor));
    if (found >= 0) return found;
  }
  const chunk = Number(resume.chunk) || 0;
  if (chunk > 0 && chunk < sentences.length) return chunk;
  return 0;
}
