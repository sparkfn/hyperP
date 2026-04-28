import { redirect } from "next/navigation";

export default function HomePage(): never {
  redirect("/persons");
}
// build test
// verify cache 2
// verify cache 3
